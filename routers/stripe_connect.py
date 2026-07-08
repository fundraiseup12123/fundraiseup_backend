from __future__ import annotations

import os
from typing import Annotated, Any
from urllib.parse import urlencode

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from auth import AuthUser, require_auth, require_org_access
from db import rest_get, rest_get_one, rest_insert, rest_patch

from frontend_url import resolve_frontend_url

router = APIRouter(prefix="/stripe", tags=["stripe"])

STRIPE_CONNECT_CLIENT_ID = os.getenv("STRIPE_CONNECT_CLIENT_ID", "")

EXPRESS_ACCOUNT_CAPABILITIES = {
    "card_payments": {"requested": True},
    "transfers": {"requested": True},
}


@router.get("/connect/status")
def connect_status() -> dict[str, Any]:
    return {
        "configured": bool(STRIPE_CONNECT_CLIENT_ID),
        "redirect_uri": f"{resolve_frontend_url()}/api/stripe/callback",
        "platform_mode": not bool(STRIPE_CONNECT_CLIENT_ID),
    }


class ConnectStartRequest(BaseModel):
    organization_id: str
    campaign_id: str | None = None
    is_default: bool = True
    frontend_origin: str | None = None


def _resolve_stripe_account(org_id: str, campaign_id: str | None) -> str | None:
    if campaign_id:
        campaign = rest_get_one(
            "campaigns",
            params={"id": f"eq.{campaign_id}", "organization_id": f"eq.{org_id}", "select": "stripe_account_id"},
        )
        if campaign and campaign.get("stripe_account_id"):
            acct = rest_get_one(
                "stripe_accounts",
                params={"id": f"eq.{campaign['stripe_account_id']}", "select": "stripe_account_id,connection_status"},
            )
            if acct and acct.get("stripe_account_id") and acct.get("connection_status") in ("active", "pending"):
                return acct["stripe_account_id"]

    default = rest_get_one(
        "stripe_accounts",
        params={
            "organization_id": f"eq.{org_id}",
            "is_default": "eq.true",
            "select": "stripe_account_id,connection_status",
        },
    )
    if default and default.get("connection_status") in ("active", "pending"):
        return default["stripe_account_id"]
    return None


@router.post("/connect/start")
def start_connect(
    payload: ConnectStartRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, str]:
    require_org_access(payload.organization_id, user, min_role="admin")

    frontend_url = resolve_frontend_url(payload.frontend_origin)

    return_path = "/admin/settings/payment-methods?connected=1"
    if payload.campaign_id:
        return_path = f"/admin/campaigns/{payload.campaign_id}/edit?step=payments&connected=1"

    return_url = f"{frontend_url}{return_path}"
    refresh_url = return_url

    if STRIPE_CONNECT_CLIENT_ID:
        state = f"{payload.organization_id}:{payload.campaign_id or ''}:{int(payload.is_default)}"
        params = {
            "response_type": "code",
            "client_id": STRIPE_CONNECT_CLIENT_ID,
            "scope": "read_write",
            "redirect_uri": f"{frontend_url}/api/stripe/callback",
            "state": state,
        }
        return {"url": f"https://connect.stripe.com/oauth/authorize?{urlencode(params)}"}

    existing = rest_get_one(
        "stripe_accounts",
        params={
            "organization_id": f"eq.{payload.organization_id}",
            "campaign_id": f"eq.{payload.campaign_id}" if payload.campaign_id else "is.null",
            "select": "stripe_account_id",
            "order": "created_at.desc",
        },
    )
    account_id = existing.get("stripe_account_id") if existing else None

    if not account_id:
        try:
            account = stripe.Account.create(
                type="express",
                capabilities=EXPRESS_ACCOUNT_CAPABILITIES,
            )
        except stripe.error.StripeError as exc:
            raise HTTPException(
                status_code=502,
                detail=str(exc.user_message or exc),
            ) from exc
        account_id = account.id
        rest_insert(
            "stripe_accounts",
            {
                "organization_id": payload.organization_id,
                "campaign_id": payload.campaign_id or None,
                "stripe_account_id": account_id,
                "is_default": payload.is_default and not payload.campaign_id,
                "connection_status": "pending",
                "charges_enabled": False,
                "payouts_enabled": False,
            },
        )

    try:
        link = stripe.AccountLink.create(
            account=account_id,
            refresh_url=refresh_url,
            return_url=return_url,
            type="account_onboarding",
        )
    except stripe.error.StripeError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc.user_message or exc),
        ) from exc
    return {"url": link.url}


@router.get("/callback")
def stripe_callback(
    code: str = Query(...),
    state: str = Query(...),
) -> RedirectResponse:
    if state.startswith("root:"):
        from routers.payment_accounts import handle_root_stripe_oauth_callback

        return handle_root_stripe_oauth_callback(code, state)

    if not STRIPE_CONNECT_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Stripe Connect not configured")

    secret = os.getenv("STRIPE_SECRET_KEY", "")
    try:
        response = stripe.OAuth.token(grant_type="authorization_code", code=code)
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stripe_account_id = response.get("stripe_user_id")
    if not stripe_account_id:
        raise HTTPException(status_code=400, detail="No Stripe account returned")

    parts = state.split(":")
    org_id = parts[0]
    campaign_id = parts[1] if len(parts) > 1 and parts[1] else None
    is_default = len(parts) > 2 and parts[2] == "1"

    account = stripe.Account.retrieve(stripe_account_id)
    row = rest_insert(
        "stripe_accounts",
        {
            "organization_id": org_id,
            "campaign_id": campaign_id or None,
            "stripe_account_id": stripe_account_id,
            "is_default": is_default and not campaign_id,
            "connection_status": "active" if account.charges_enabled else "pending",
            "charges_enabled": bool(account.charges_enabled),
            "payouts_enabled": bool(account.payouts_enabled),
        },
    )

    if campaign_id and row:
        rest_patch("campaigns", {"stripe_account_id": row["id"]}, match={"id": campaign_id})

    redirect_path = "/admin/settings/payment-methods?connected=1"
    if campaign_id:
        redirect_path = f"/admin/campaigns/{campaign_id}/edit?step=payments&connected=1"

    return RedirectResponse(url=f"{resolve_frontend_url()}{redirect_path}")


@router.get("/orgs/{org_id}/accounts")
def list_stripe_accounts(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> list[dict[str, Any]]:
    require_org_access(org_id, user, min_role="member")
    return rest_get("stripe_accounts", params={"organization_id": f"eq.{org_id}", "select": "*"})


def resolve_stripe_account_for_checkout(org_id: str, campaign_id: str) -> tuple[str | None, dict[str, Any] | None]:
    account_id = _resolve_stripe_account(org_id, campaign_id)
    if not account_id:
        return None, None
    return account_id, {"stripe_account": account_id}
