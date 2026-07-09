from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field

from auth import AuthUser, require_auth, require_org_access
from db import rest_delete, rest_get, rest_get_one, rest_insert, rest_patch
from frontend_url import resolve_frontend_url
from paypal_client import (
    build_paypal_connect_url,
    build_paypal_hosted_connect_url,
    exchange_paypal_code,
    paypal_configured,
)

router = APIRouter(prefix="/paypal", tags=["paypal-connect"])


def paypal_redirect_uri(frontend_url: str | None = None) -> str:
    return f"{resolve_frontend_url(frontend_url)}/api/paypal/callback"


def get_paypal_connect_url(state: str, frontend_url: str | None = None) -> str:
    base = resolve_frontend_url(frontend_url)
    redirect_uri = paypal_redirect_uri(base)

    if paypal_client_id() and not paypal_client_secret():
        raise HTTPException(
            status_code=503,
            detail=(
                "PayPal Connect requires PAYPAL_CLIENT_SECRET on the backend. "
                "Add PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in Railway, then restart the API."
            ),
        )

    return build_paypal_connect_url(
        state=state,
        redirect_uri=redirect_uri,
        frontend_url=base,
    )


class PayPalConnectStartRequest(BaseModel):
    organization_id: str
    campaign_id: str | None = None
    is_default: bool = True
    frontend_origin: str | None = None


class PayPalConnectCompleteRequest(BaseModel):
    state: str = Field(min_length=5, max_length=512)
    email: EmailStr
    merchant_id: str | None = Field(default=None, max_length=128)
    frontend_origin: str | None = None


@router.get("/connect/status")
def paypal_connect_status() -> dict[str, Any]:
    return {
        "configured": paypal_configured(),
        "connect_available": True,
        "mode": "api" if paypal_configured() else "hosted",
        "redirect_uri": paypal_redirect_uri(),
    }


@router.post("/connect/start")
def start_paypal_connect(
    payload: PayPalConnectStartRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, str]:
    require_org_access(payload.organization_id, user, min_role="admin")
    state = f"org:{payload.organization_id}:{payload.campaign_id or ''}:{int(payload.is_default)}"
    return {"url": get_paypal_connect_url(state, payload.frontend_origin)}


@router.post("/connect/complete")
def complete_paypal_connect(
    payload: PayPalConnectCompleteRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, str]:
    merchant_id = (payload.merchant_id or payload.email).strip()
    email = str(payload.email).strip()
    frontend_base = resolve_frontend_url(payload.frontend_origin)

    if payload.state.startswith("root:"):
        from routers.payment_accounts import save_root_paypal_account

        if user.role != "super_admin":
            raise HTTPException(status_code=403, detail="Super admin access required")
        redirect = save_root_paypal_account(payload.state, merchant_id, email)
        if redirect.startswith("/"):
            redirect = f"{frontend_base}{redirect}"
        return {"redirect": redirect}

    if not payload.state.startswith("org:"):
        raise HTTPException(status_code=400, detail="Invalid PayPal connect state")

    parts = payload.state.split(":")
    if len(parts) < 4:
        raise HTTPException(status_code=400, detail="Invalid PayPal connect state")

    org_id = parts[1]
    campaign_id = parts[2] or None
    is_default = parts[3] == "1"
    require_org_access(org_id, user, min_role="admin")

    row = rest_insert(
        "paypal_accounts",
        {
            "organization_id": org_id,
            "campaign_id": campaign_id,
            "paypal_merchant_id": merchant_id,
            "paypal_email": email,
            "is_default": is_default and not campaign_id,
            "connection_status": "active",
        },
    )
    if not row:
        raise HTTPException(status_code=500, detail="Unable to save PayPal account")

    if campaign_id:
        rest_patch("campaigns", {"paypal_account_id": row["id"]}, match={"id": campaign_id})
        return {"redirect": f"{frontend_base}/admin/campaigns/{campaign_id}/edit?step=payments&connected=1&provider=paypal"}

    return {"redirect": f"{frontend_base}/admin/settings/payment-methods?connected=1&provider=paypal"}


@router.get("/orgs/{org_id}/accounts")
def list_paypal_accounts(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> list[dict[str, Any]]:
    require_org_access(org_id, user, min_role="member")
    return rest_get("paypal_accounts", params={"organization_id": f"eq.{org_id}", "select": "*"})


@router.delete("/accounts/{account_id}")
def disconnect_paypal_account(
    account_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, bool]:
    account = rest_get_one(
        "paypal_accounts",
        params={"id": f"eq.{account_id}", "select": "id,organization_id,campaign_id"},
    )
    if not account:
        raise HTTPException(status_code=404, detail="PayPal account not found")

    require_org_access(account["organization_id"], user, min_role="admin")

    if account.get("campaign_id"):
        campaign = rest_get_one(
            "campaigns",
            params={"id": f"eq.{account['campaign_id']}", "select": "paypal_account_id"},
        )
        if campaign and campaign.get("paypal_account_id") == account_id:
            rest_patch("campaigns", {"paypal_account_id": None}, match={"id": account["campaign_id"]})

    if not rest_delete("paypal_accounts", match={"id": account_id}):
        raise HTTPException(status_code=500, detail="Unable to remove PayPal account")

    return {"removed": True}


def _save_org_paypal_account(
    *,
    org_id: str,
    campaign_id: str | None,
    is_default: bool,
    merchant_id: str,
    email: str | None,
) -> RedirectResponse:
    row = rest_insert(
        "paypal_accounts",
        {
            "organization_id": org_id,
            "campaign_id": campaign_id,
            "paypal_merchant_id": merchant_id,
            "paypal_email": email,
            "is_default": is_default and not campaign_id,
            "connection_status": "active",
        },
    )

    if campaign_id and row:
        rest_patch("campaigns", {"paypal_account_id": row["id"]}, match={"id": campaign_id})

    if campaign_id:
        redirect_path = f"/admin/campaigns/{campaign_id}/edit?step=payments&connected=1&provider=paypal"
    else:
        redirect_path = "/admin/settings/payment-methods?connected=1&provider=paypal"

    return RedirectResponse(url=f"{resolve_frontend_url()}{redirect_path}")


def handle_paypal_partner_callback(state: str, merchant_id: str) -> RedirectResponse | None:
    if state.startswith("root:"):
        from routers.payment_accounts import handle_root_paypal_partner_callback

        return handle_root_paypal_partner_callback(state, merchant_id)

    if state.startswith("org:"):
        parts = state.split(":")
        if len(parts) < 4:
            raise HTTPException(status_code=400, detail="Invalid PayPal connect state")

        org_id = parts[1]
        campaign_id = parts[2] or None
        is_default = parts[3] == "1"
        return _save_org_paypal_account(
            org_id=org_id,
            campaign_id=campaign_id,
            is_default=is_default,
            merchant_id=merchant_id,
            email=None,
        )

    return None


def handle_org_paypal_callback(code: str, state: str) -> RedirectResponse | None:
    if not state.startswith("org:"):
        return None

    parts = state.split(":")
    if len(parts) < 4:
        raise HTTPException(status_code=400, detail="Invalid PayPal connect state")

    org_id = parts[1]
    campaign_id = parts[2] or None
    is_default = parts[3] == "1"

    merchant_id, email = exchange_paypal_code(code, paypal_redirect_uri())
    return _save_org_paypal_account(
        org_id=org_id,
        campaign_id=campaign_id,
        is_default=is_default,
        merchant_id=merchant_id,
        email=email,
    )


def resolve_paypal_payee_email_for_checkout(campaign_id: str | None, checkout_view: str | None) -> str | None:
    from site_constants import ROOT_CAMPAIGN_ID

    def pick_email(acct: dict[str, Any] | None) -> str | None:
        if not acct or acct.get("connection_status") not in ("active", "pending", None):
            return None
        email = acct.get("paypal_email")
        merchant = acct.get("paypal_merchant_id")
        if email and "@" in str(email):
            return str(email).strip()
        if merchant and "@" in str(merchant):
            return str(merchant).strip()
        return None

    if campaign_id and campaign_id != ROOT_CAMPAIGN_ID:
        campaign = rest_get_one(
            "campaigns",
            params={"id": f"eq.{campaign_id}", "select": "id,organization_id,paypal_account_id"},
        )
        if campaign:
            org_id = campaign["organization_id"]
            if campaign.get("paypal_account_id"):
                acct = rest_get_one(
                    "paypal_accounts",
                    params={"id": f"eq.{campaign['paypal_account_id']}", "select": "paypal_email,paypal_merchant_id,connection_status"},
                )
                picked = pick_email(acct)
                if picked:
                    return picked

            default = rest_get_one(
                "paypal_accounts",
                params={
                    "organization_id": f"eq.{org_id}",
                    "is_default": "eq.true",
                    "select": "paypal_email,paypal_merchant_id,connection_status",
                },
            )
            picked = pick_email(default)
            if picked:
                return picked

    from routers.payment_accounts import resolve_root_paypal_payee

    return resolve_root_paypal_payee(checkout_view)


def resolve_paypal_merchant_for_checkout(org_id: str, campaign_id: str | None) -> str | None:
    if campaign_id:
        campaign = rest_get_one(
            "campaigns",
            params={"id": f"eq.{campaign_id}", "organization_id": f"eq.{org_id}", "select": "paypal_account_id"},
        )
        if campaign and campaign.get("paypal_account_id"):
            acct = rest_get_one(
                "paypal_accounts",
                params={"id": f"eq.{campaign['paypal_account_id']}", "select": "paypal_merchant_id,connection_status"},
            )
            if acct and acct.get("connection_status") == "active":
                return acct.get("paypal_merchant_id")

    default = rest_get_one(
        "paypal_accounts",
        params={
            "organization_id": f"eq.{org_id}",
            "is_default": "eq.true",
            "select": "paypal_merchant_id,connection_status",
        },
    )
    if default and default.get("connection_status") == "active":
        return default.get("paypal_merchant_id")
    return None
