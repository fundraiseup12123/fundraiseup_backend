from __future__ import annotations

import json
import os
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

import stripe
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from auth import AuthUser, require_super_admin
from db import rest_get_one, rest_insert, rest_patch
from site_constants import ROOT_CAMPAIGN_ID

from frontend_url import resolve_frontend_url
from routers.stripe_connect import use_stripe_standard_oauth

router = APIRouter(prefix="/super/payment-accounts", tags=["payment-accounts"])

STRIPE_CONNECT_CLIENT_ID = os.getenv("STRIPE_CONNECT_CLIENT_ID", "")
PaymentView = Literal["homepage", "popup"]

EXPRESS_ACCOUNT_CAPABILITIES = {
    "card_payments": {"requested": True},
    "transfers": {"requested": True},
}


class PaymentViewPayload(BaseModel):
    view: PaymentView
    frontend_origin: str | None = None


class PaymentAccountView(BaseModel):
    view: PaymentView
    stripe_account_id: str | None = None
    stripe_connection_status: str | None = None
    stripe_charges_enabled: bool = False
    paypal_merchant_id: str | None = None
    paypal_connection_status: str | None = None


def _default_accounts() -> dict[str, dict[str, Any]]:
    return {
        "homepage": {
            "stripe_account_id": None,
            "stripe_connection_status": None,
            "stripe_charges_enabled": False,
            "paypal_merchant_id": None,
            "paypal_connection_status": None,
        },
        "popup": {
            "stripe_account_id": None,
            "stripe_connection_status": None,
            "stripe_charges_enabled": False,
            "paypal_merchant_id": None,
            "paypal_connection_status": None,
        },
    }


def _load_accounts_raw() -> dict[str, dict[str, Any]]:
    content = rest_get_one(
        "campaign_content",
        params={"campaign_id": f"eq.{ROOT_CAMPAIGN_ID}", "select": "payment_accounts_json"},
    )
    raw = (content or {}).get("payment_accounts_json")
    if not raw:
        return _default_accounts()
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(parsed, dict):
            return _default_accounts()
        merged = _default_accounts()
        for view in ("homepage", "popup"):
            if isinstance(parsed.get(view), dict):
                merged[view].update(parsed[view])
        return merged
    except (json.JSONDecodeError, TypeError):
        return _default_accounts()


def _save_accounts(accounts: dict[str, dict[str, Any]]) -> None:
    payload = {"payment_accounts_json": json.dumps(accounts)}
    result = rest_patch(
        "campaign_content",
        payload,
        match={"campaign_id": ROOT_CAMPAIGN_ID},
    )
    if result is not None:
        return

    inserted = rest_insert(
        "campaign_content",
        {"campaign_id": ROOT_CAMPAIGN_ID, **payload},
    )
    if not inserted:
        raise HTTPException(
            status_code=503,
            detail="Could not save payment accounts. Run backend/sql/006_payment_accounts_json.sql in Supabase.",
        )


def _refresh_stripe_view(view: PaymentView, accounts: dict[str, dict[str, Any]]) -> None:
    account_id = accounts[view].get("stripe_account_id")
    if not account_id:
        return
    try:
        account = stripe.Account.retrieve(account_id)
        accounts[view]["stripe_connection_status"] = "active" if account.charges_enabled else "pending"
        accounts[view]["stripe_charges_enabled"] = bool(account.charges_enabled)
    except stripe.error.StripeError:
        accounts[view]["stripe_connection_status"] = "restricted"


def _accounts_response(accounts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"view": "homepage", **accounts["homepage"]},
        {"view": "popup", **accounts["popup"]},
    ]


@router.get("/status")
def payment_accounts_status(
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    return {
        "stripe_configured": bool(stripe.api_key),
        "paypal_configured": bool(
            os.getenv("PAYPAL_CLIENT_ID") or os.getenv("NEXT_PUBLIC_PAYPAL_CLIENT_ID")
        ),
        "stripe_redirect_uri": f"{resolve_frontend_url()}/api/stripe/callback",
        "paypal_redirect_uri": f"{resolve_frontend_url()}/api/paypal/callback",
    }


@router.get("")
def list_payment_accounts(
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> list[dict[str, Any]]:
    accounts = _load_accounts_raw()
    for view in ("homepage", "popup"):
        _refresh_stripe_view(view, accounts)  # type: ignore[arg-type]
    _save_accounts(accounts)
    return _accounts_response(accounts)


def resolve_root_stripe_account(checkout_view: str | None) -> str | None:
    view: PaymentView = "popup" if checkout_view == "popup" else "homepage"
    accounts = _load_accounts_raw()
    entry = accounts.get(view, {})
    account_id = entry.get("stripe_account_id")
    if not account_id:
        return None
    if entry.get("stripe_connection_status") in ("active", "pending"):
        return account_id
    return None


def resolve_root_paypal_payee(checkout_view: str | None) -> str | None:
    view: PaymentView = "popup" if checkout_view == "popup" else "homepage"
    accounts = _load_accounts_raw()
    entry = accounts.get(view, {})
    email = entry.get("paypal_email")
    merchant = entry.get("paypal_merchant_id")
    if not email and not merchant:
        return None
    status = entry.get("paypal_connection_status")
    if status and status not in ("active", "pending", "connected", None):
        return None
    if email and "@" in str(email):
        return str(email).strip()
    if merchant and "@" in str(merchant):
        return str(merchant).strip()
    return None


@router.post("/stripe/connect/start")
def start_root_stripe_connect(
    payload: PaymentViewPayload,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, str]:
    accounts = _load_accounts_raw()
    view = payload.view
    entry = accounts[view]
    account_id = entry.get("stripe_account_id")
    frontend_url = resolve_frontend_url(payload.frontend_origin)

    return_url = (
        f"{frontend_url}/super-admin/payment-accounts"
        f"?connected=1&provider=stripe&view={view}"
    )
    refresh_url = (
        f"{frontend_url}/super-admin/payment-accounts"
        f"?refresh=1&provider=stripe&view={view}"
    )

    if STRIPE_CONNECT_CLIENT_ID and use_stripe_standard_oauth():
        state = f"root:{view}"
        params = {
            "response_type": "code",
            "client_id": STRIPE_CONNECT_CLIENT_ID,
            "scope": "read_write",
            "redirect_uri": f"{frontend_url}/api/stripe/callback",
            "state": state,
        }
        return {"url": f"https://connect.stripe.com/oauth/authorize?{urlencode(params)}"}

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
        entry["stripe_account_id"] = account_id
        entry["stripe_connection_status"] = "pending"
        entry["stripe_charges_enabled"] = False
        accounts[view] = entry
        _save_accounts(accounts)

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


@router.post("/paypal/connect/start")
def start_root_paypal_connect(
    payload: PaymentViewPayload,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, str]:
    from routers.paypal_connect import get_paypal_connect_url

    view = payload.view
    state = f"root:{view}"
    try:
        url = get_paypal_connect_url(state, resolve_frontend_url(payload.frontend_origin))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Unable to start PayPal connect: {exc}",
        ) from exc
    return {"url": url}


def handle_root_stripe_oauth_callback(code: str, state: str) -> RedirectResponse:
    if not state.startswith("root:"):
        raise HTTPException(status_code=400, detail="Invalid state")

    view = state.split(":", 1)[1]
    if view not in ("homepage", "popup"):
        raise HTTPException(status_code=400, detail="Invalid view")

    try:
        response = stripe.OAuth.token(grant_type="authorization_code", code=code)
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stripe_account_id = response.get("stripe_user_id")
    if not stripe_account_id:
        raise HTTPException(status_code=400, detail="No Stripe account returned")

    account = stripe.Account.retrieve(stripe_account_id)
    accounts = _load_accounts_raw()
    accounts[view] = {
        **accounts[view],
        "stripe_account_id": stripe_account_id,
        "stripe_connection_status": "active" if account.charges_enabled else "pending",
        "stripe_charges_enabled": bool(account.charges_enabled),
    }
    _save_accounts(accounts)

    return RedirectResponse(
        url=f"{resolve_frontend_url()}/super-admin/payment-accounts?connected=1&provider=stripe&view={view}"
    )


def save_root_paypal_account(state: str, merchant_id: str, email: str | None = None) -> str:
    if not state.startswith("root:"):
        raise HTTPException(status_code=400, detail="Invalid state")

    view = state.split(":", 1)[1]
    if view not in ("homepage", "popup"):
        raise HTTPException(status_code=400, detail="Invalid view")

    accounts = _load_accounts_raw()
    accounts[view] = {
        **accounts[view],
        "paypal_merchant_id": merchant_id,
        "paypal_email": email,
        "paypal_connection_status": "active",
    }
    _save_accounts(accounts)
    return f"/super-admin/payment-accounts?connected=1&provider=paypal&view={view}"


def handle_root_paypal_partner_callback(state: str, merchant_id: str) -> RedirectResponse:
    redirect_url = save_root_paypal_account(state, merchant_id)
    if redirect_url.startswith("/"):
        redirect_url = f"{resolve_frontend_url()}{redirect_url}"
    return RedirectResponse(url=redirect_url)


@router.post("/paypal/disconnect")
def disconnect_root_paypal(
    payload: PaymentViewPayload,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, bool]:
    accounts = _load_accounts_raw()
    view = payload.view
    accounts[view] = {
        **accounts[view],
        "paypal_merchant_id": None,
        "paypal_email": None,
        "paypal_connection_status": None,
    }
    _save_accounts(accounts)
    return {"removed": True}


@router.post("/stripe/disconnect")
def disconnect_root_stripe(
    payload: PaymentViewPayload,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, bool]:
    accounts = _load_accounts_raw()
    view = payload.view
    accounts[view] = {
        **accounts[view],
        "stripe_account_id": None,
        "stripe_connection_status": None,
        "stripe_charges_enabled": False,
    }
    _save_accounts(accounts)
    return {"removed": True}


def handle_root_paypal_callback(code: str, state: str) -> RedirectResponse | None:
    if not state.startswith("root:"):
        return None

    from routers.paypal_connect import exchange_paypal_code

    view = state.split(":", 1)[1]
    if view not in ("homepage", "popup"):
        raise HTTPException(status_code=400, detail="Invalid view")

    merchant_id, email = exchange_paypal_code(code, f"{resolve_frontend_url()}/api/paypal/callback")
    redirect_url = save_root_paypal_account(state, merchant_id, email)
    if redirect_url.startswith("/"):
        redirect_url = f"{resolve_frontend_url()}{redirect_url}"
    return RedirectResponse(url=redirect_url)
