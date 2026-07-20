from __future__ import annotations

import os
from typing import Annotated, Any
from urllib.parse import quote, urlencode

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from auth import AuthUser, require_auth, require_org_access
from db import rest_delete, rest_get, rest_get_one, rest_insert, rest_insert_result, rest_patch, rest_patch_result

from frontend_url import pack_origin_token, resolve_frontend_url, unpack_origin_token

router = APIRouter(prefix="/stripe", tags=["stripe"])

STRIPE_CONNECT_CLIENT_ID = (os.getenv("STRIPE_CONNECT_CLIENT_ID", "") or "").strip().strip('"').strip("'")


def use_stripe_standard_oauth() -> bool:
    """Use Stripe OAuth account picker when a Connect client ID is configured."""
    if not STRIPE_CONNECT_CLIENT_ID:
        return False
    if os.getenv("STRIPE_CONNECT_USE_EXPRESS", "").lower() in ("1", "true", "yes"):
        return False
    if os.getenv("STRIPE_CONNECT_USE_STANDARD_OAUTH", "").lower() in ("0", "false", "no"):
        return False
    return True


def build_stripe_oauth_authorize_url(*, state: str, frontend_url: str) -> str:
    """Stripe Connect OAuth v2 — pick an existing account or create one (Fundraise Up style)."""
    params = {
        "response_type": "code",
        "client_id": STRIPE_CONNECT_CLIENT_ID,
        "scope": "read_write",
        "redirect_uri": f"{frontend_url.rstrip('/')}/api/stripe/callback",
        "state": state,
        "always_prompt": "true",
    }
    return f"https://connect.stripe.com/oauth/v2/authorize?{urlencode(params)}"

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
        "oauth_mode": bool(STRIPE_CONNECT_CLIENT_ID and use_stripe_standard_oauth()),
        "oauth_version": "v2" if use_stripe_standard_oauth() else None,
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

    if STRIPE_CONNECT_CLIENT_ID and use_stripe_standard_oauth():
        state = (
            f"{payload.organization_id}:{payload.campaign_id or ''}:"
            f"{int(payload.is_default)}:{pack_origin_token(frontend_url)}"
        )
        return {"url": build_stripe_oauth_authorize_url(state=state, frontend_url=frontend_url)}

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

    parts = state.split(":")
    org_id = parts[0]
    campaign_id = parts[1] if len(parts) > 1 and parts[1] else None
    is_default = len(parts) > 2 and parts[2] == "1"
    frontend_origin = unpack_origin_token(parts[3]) if len(parts) > 3 else None
    frontend_url = resolve_frontend_url(frontend_origin)

    def fail(message: str) -> RedirectResponse:
        return RedirectResponse(
            url=f"{frontend_url}/admin/settings/payment-methods?error={quote(message[:180], safe='')}"
        )

    try:
        response = stripe.OAuth.token(grant_type="authorization_code", code=code)
    except stripe.error.StripeError as exc:
        return fail(str(exc.user_message or exc) or "Stripe authorization failed")

    stripe_account_id = getattr(response, "stripe_user_id", None)
    if not stripe_account_id:
        try:
            stripe_account_id = response["stripe_user_id"]
        except Exception:
            stripe_account_id = None
    if not stripe_account_id:
        return fail("No Stripe account returned from OAuth")

    try:
        account = stripe.Account.retrieve(stripe_account_id)
        charges_enabled = bool(getattr(account, "charges_enabled", False))
        payouts_enabled = bool(getattr(account, "payouts_enabled", False))
    except stripe.error.StripeError:
        # OAuth succeeded; account details may lag — still persist the connection.
        charges_enabled = False
        payouts_enabled = False

    payload = {
        "organization_id": org_id,
        "campaign_id": campaign_id or None,
        "stripe_account_id": stripe_account_id,
        "is_default": is_default and not campaign_id,
        "connection_status": "active" if charges_enabled else "pending",
        "charges_enabled": charges_enabled,
        "payouts_enabled": payouts_enabled,
    }

    existing = rest_get_one(
        "stripe_accounts",
        params={"stripe_account_id": f"eq.{stripe_account_id}", "select": "id"},
    )
    if existing:
        row, save_error = rest_patch_result("stripe_accounts", payload, match={"id": existing["id"]})
    else:
        row, save_error = rest_insert_result(
            "stripe_accounts",
            payload,
            on_conflict="stripe_account_id",
        )
        if not row and save_error:
            row, save_error = rest_insert_result("stripe_accounts", payload)

    if not row:
        return fail(save_error or "Unable to save Stripe account")

    row_id = row.get("id") if isinstance(row, dict) else None
    if campaign_id and row_id:
        rest_patch("campaigns", {"stripe_account_id": row_id}, match={"id": campaign_id})

    redirect_path = "/admin/settings/payment-methods?connected=1"
    if campaign_id:
        redirect_path = f"/admin/campaigns/{campaign_id}/edit?step=payments&connected=1"

    return RedirectResponse(url=f"{frontend_url}{redirect_path}")


@router.get("/orgs/{org_id}/accounts")
def list_stripe_accounts(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> list[dict[str, Any]]:
    require_org_access(org_id, user, min_role="member")
    return rest_get("stripe_accounts", params={"organization_id": f"eq.{org_id}", "select": "*"})


@router.delete("/accounts/{account_id}")
def disconnect_stripe_account(
    account_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, bool]:
    account = rest_get_one(
        "stripe_accounts",
        params={"id": f"eq.{account_id}", "select": "id,organization_id,campaign_id"},
    )
    if not account:
        raise HTTPException(status_code=404, detail="Stripe account not found")

    require_org_access(account["organization_id"], user, min_role="admin")

    if account.get("campaign_id"):
        campaign = rest_get_one(
            "campaigns",
            params={"id": f"eq.{account['campaign_id']}", "select": "stripe_account_id"},
        )
        if campaign and campaign.get("stripe_account_id") == account_id:
            rest_patch("campaigns", {"stripe_account_id": None}, match={"id": account["campaign_id"]})

    if not rest_delete("stripe_accounts", match={"id": account_id}):
        raise HTTPException(status_code=500, detail="Unable to remove Stripe account")

    return {"removed": True}


def stripe_account_accessible(account_id: str | None) -> bool:
    """True when the platform secret key can act on this connected account."""
    if not account_id:
        return False
    try:
        stripe.Account.retrieve(account_id)
        return True
    except stripe.error.PermissionError:
        return False
    except stripe.error.InvalidRequestError as exc:
        message = str(exc).lower()
        if "does not have access" in message or "no such account" in message:
            return False
        raise
    except stripe.error.StripeError:
        return False


def resolve_stripe_account_for_checkout(org_id: str, campaign_id: str) -> tuple[str | None, dict[str, Any] | None]:
    from routers.payment_accounts import resolve_root_stripe_account, uses_platform_provider

    if uses_platform_provider(org_id, "stripe", campaign_id):
        account_id = resolve_root_stripe_account("homepage")
        if not account_id:
            return None, None
        return account_id, {"stripe_account": account_id}

    account_id = _resolve_stripe_account(org_id, campaign_id)
    if not account_id:
        return None, None
    if not stripe_account_accessible(account_id):
        return None, None
    return account_id, {"stripe_account": account_id}
