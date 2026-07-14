from __future__ import annotations

import os
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from auth import AuthUser, require_auth, require_org_access
from currency import estimate_processing_fee, format_display_amount
from db import rest_delete, rest_get, rest_get_one, rest_insert, rest_patch
from frontend_url import resolve_frontend_url
from nowpayments_client import (
    api_key_hint,
    create_invoice,
    verify_api_key,
    verify_ipn_signature,
)
from supabase_client import insert_donation, supabase_enabled

router = APIRouter(prefix="/nowpayments", tags=["nowpayments"])


class DonorDetails(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=254)
    phone: str | None = None


class UtmParams(BaseModel):
    source: str | None = None
    medium: str | None = None
    campaign: str | None = None
    term: str | None = None
    content: str | None = None


class DeviceInfo(BaseModel):
    os: str | None = None
    browser: str | None = None
    type: str | None = None
    country: str | None = None
    city: str | None = None
    gender: str | None = None


class AttachNowPaymentsRequest(BaseModel):
    api_key: str = Field(min_length=8, max_length=256)
    ipn_secret: str = Field(min_length=8, max_length=256)
    email: str | None = None
    is_default: bool = True
    campaign_id: str | None = None


class PrepareNowPaymentsRedirectRequest(BaseModel):
    amount: float = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    frequency: Literal["once", "monthly"] = "once"
    cover_fees: bool = False
    dedicate: bool = False
    honoree_name: str | None = None
    comment: str | None = None
    campaign_id: str | None = None
    checkout_view: Literal["homepage", "popup"] = "homepage"
    donor: DonorDetails
    utm: UtmParams | None = None
    device: DeviceInfo | None = None
    return_url: str | None = None
    cancel_url: str | None = None


class CompleteNowPaymentsRedirectRequest(PrepareNowPaymentsRedirectRequest):
    payment_ref: str = Field(min_length=5, max_length=128)
    invoice_id: str | None = None


def _public_account(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "organization_id": row.get("organization_id"),
        "campaign_id": row.get("campaign_id"),
        "api_key_hint": row.get("api_key_hint") or api_key_hint(str(row.get("api_key") or "")),
        "email": row.get("email"),
        "is_default": bool(row.get("is_default")),
        "connection_status": row.get("connection_status") or "active",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _ipn_callback_url() -> str:
    explicit = (os.getenv("PUBLIC_API_URL") or os.getenv("API_PUBLIC_URL") or "").strip().rstrip("/")
    if explicit:
        return f"{explicit}/nowpayments/ipn"
    return f"{resolve_frontend_url()}/api/backend/nowpayments/ipn"


def _resolve_total(amount: float, currency: str, cover_fees: bool) -> tuple[float, float]:
    from currency import calculate_total_with_fees

    base = float(amount)
    if cover_fees:
        return base, calculate_total_with_fees(base, currency)
    return base, base


def _resolve_organization_id(campaign_id: str | None) -> str:
    from site_constants import ROOT_CAMPAIGN_ID, ROOT_ORG_ID

    if campaign_id:
        campaign = rest_get_one(
            "campaigns",
            params={"id": f"eq.{campaign_id}", "select": "organization_id"},
        )
        if campaign and campaign.get("organization_id"):
            return str(campaign["organization_id"])
        if campaign_id == ROOT_CAMPAIGN_ID:
            return ROOT_ORG_ID
    return ROOT_ORG_ID


def resolve_nowpayments_account_for_checkout(
    campaign_id: str | None,
    checkout_view: str | None,
) -> dict[str, Any] | None:
    from site_constants import ROOT_CAMPAIGN_ID

    def pick(acct: dict[str, Any] | None) -> dict[str, Any] | None:
        if not acct:
            return None
        if acct.get("connection_status") not in ("active", "pending", None):
            return None
        if not acct.get("api_key"):
            return None
        return acct

    if campaign_id and campaign_id != ROOT_CAMPAIGN_ID:
        campaign = rest_get_one(
            "campaigns",
            params={"id": f"eq.{campaign_id}", "select": "id,organization_id,nowpayments_account_id"},
        )
        if campaign:
            org_id = campaign["organization_id"]
            if campaign.get("nowpayments_account_id"):
                acct = rest_get_one(
                    "nowpayments_accounts",
                    params={"id": f"eq.{campaign['nowpayments_account_id']}", "select": "*"},
                )
                picked = pick(acct)
                if picked:
                    return picked

            default = rest_get_one(
                "nowpayments_accounts",
                params={
                    "organization_id": f"eq.{org_id}",
                    "is_default": "eq.true",
                    "select": "*",
                },
            )
            picked = pick(default)
            if picked:
                return picked

    from routers.payment_accounts import resolve_root_nowpayments_account

    return pick(resolve_root_nowpayments_account(checkout_view))


def _clear_org_defaults(org_id: str) -> None:
    defaults = rest_get(
        "nowpayments_accounts",
        params={
            "organization_id": f"eq.{org_id}",
            "is_default": "eq.true",
            "select": "id",
        },
    )
    for row in defaults:
        rest_patch("nowpayments_accounts", {"is_default": False}, match={"id": row["id"]})


def _record_nowpayments_donation(
    *,
    payment_id: str,
    payload: CompleteNowPaymentsRedirectRequest,
    base_amount: float,
    total_display: float,
    status: str = "succeeded",
) -> dict[str, object] | None:
    display_currency = payload.currency.upper()
    cover_fees = payload.cover_fees
    if cover_fees:
        processing_fee = max(0.0, round(total_display - base_amount, 2))
        payout_amount = base_amount
    else:
        processing_fee = estimate_processing_fee(base_amount, display_currency)
        payout_amount = max(0.0, round(base_amount - processing_fee, 2))

    campaign_id = payload.campaign_id
    from site_constants import ROOT_CAMPAIGN_ID

    if not campaign_id:
        campaign_id = ROOT_CAMPAIGN_ID

    row: dict[str, Any] = {
        "stripe_payment_intent_id": payment_id,
        "first_name": payload.donor.first_name,
        "last_name": payload.donor.last_name,
        "email": payload.donor.email,
        "amount": total_display,
        "base_amount": base_amount,
        "currency": display_currency,
        "frequency": payload.frequency,
        "payment_method": "nowpayments",
        "honoree_name": payload.honoree_name or None,
        "comment": payload.comment or None,
        "organization_id": _resolve_organization_id(campaign_id),
        "campaign_id": campaign_id,
        "status": status,
        "fee_covered": cover_fees,
        "platform_fee": 0,
        "processing_fee": processing_fee,
        "payout_amount": payout_amount,
    }
    if payload.device:
        device = {
            k: v
            for k, v in {
                "os": payload.device.os,
                "browser": payload.device.browser,
                "type": payload.device.type,
                "country": payload.device.country,
                "city": payload.device.city,
                "gender": payload.device.gender,
            }.items()
            if v
        }
        if device:
            row["device"] = device
    if payload.utm:
        utm = {
            k: v
            for k, v in {
                "source": payload.utm.source,
                "medium": payload.utm.medium,
                "campaign": payload.utm.campaign,
                "term": payload.utm.term,
                "content": payload.utm.content,
            }.items()
            if v
        }
        if utm:
            row["utm"] = utm

    if not supabase_enabled():
        return None
    return insert_donation(row)


@router.get("/orgs/{org_id}/accounts")
def list_nowpayments_accounts(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> list[dict[str, Any]]:
    require_org_access(org_id, user, min_role="member")
    rows = rest_get(
        "nowpayments_accounts",
        params={"organization_id": f"eq.{org_id}", "select": "*"},
    )
    return [_public_account(row) for row in rows]


@router.post("/orgs/{org_id}/accounts")
def attach_nowpayments_account(
    org_id: str,
    payload: AttachNowPaymentsRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")

    api_key = payload.api_key.strip()
    ipn_secret = payload.ipn_secret.strip()
    if not verify_api_key(api_key):
        raise HTTPException(
            status_code=400,
            detail="NOWPayments API key is invalid or the API is unreachable. Check the key and try again.",
        )

    is_default = bool(payload.is_default) and not payload.campaign_id
    if is_default:
        _clear_org_defaults(org_id)

    row = rest_insert(
        "nowpayments_accounts",
        {
            "organization_id": org_id,
            "campaign_id": payload.campaign_id,
            "api_key": api_key,
            "ipn_secret": ipn_secret,
            "api_key_hint": api_key_hint(api_key),
            "email": (payload.email or "").strip() or None,
            "is_default": is_default,
            "connection_status": "active",
        },
    )
    if not row:
        raise HTTPException(
            status_code=500,
            detail="Unable to save NOWPayments account. Run backend/sql/014_nowpayments_accounts.sql in Supabase.",
        )

    if payload.campaign_id:
        rest_patch(
            "campaigns",
            {"nowpayments_account_id": row["id"]},
            match={"id": payload.campaign_id},
        )

    return _public_account(row)


@router.delete("/accounts/{account_id}")
def disconnect_nowpayments_account(
    account_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, bool]:
    account = rest_get_one(
        "nowpayments_accounts",
        params={"id": f"eq.{account_id}", "select": "id,organization_id,campaign_id"},
    )
    if not account:
        raise HTTPException(status_code=404, detail="NOWPayments account not found")

    require_org_access(account["organization_id"], user, min_role="admin")

    if account.get("campaign_id"):
        campaign = rest_get_one(
            "campaigns",
            params={"id": f"eq.{account['campaign_id']}", "select": "nowpayments_account_id"},
        )
        if campaign and campaign.get("nowpayments_account_id") == account_id:
            rest_patch(
                "campaigns",
                {"nowpayments_account_id": None},
                match={"id": account["campaign_id"]},
            )

    if not rest_delete("nowpayments_accounts", match={"id": account_id}):
        raise HTTPException(status_code=500, detail="Unable to remove NOWPayments account")

    return {"removed": True}


@router.get("/checkout-config")
def nowpayments_checkout_config(
    campaign_id: str | None = Query(None),
    checkout_view: Literal["homepage", "popup"] = Query("homepage"),
) -> dict[str, object]:
    account = resolve_nowpayments_account_for_checkout(campaign_id, checkout_view)
    return {
        "available": bool(account),
        "merchant_connected": bool(account),
        "api_configured": bool(account),
    }


@router.post("/prepare-redirect")
def nowpayments_prepare_redirect(payload: PrepareNowPaymentsRedirectRequest) -> dict[str, str]:
    from currency import assert_meets_min_donation, resolve_min_donation_for_frequency

    if payload.frequency != "once":
        raise HTTPException(status_code=400, detail="Crypto (NOWPayments) is only available for one-time donations")

    if payload.campaign_id:
        campaign = rest_get_one(
            "campaigns",
            params={
                "id": f"eq.{payload.campaign_id}",
                "select": "min_donation_amount,min_donation_amount_once,min_donation_amount_monthly,default_currency,status",
            },
        )
        if campaign and campaign.get("status") == "live":
            assert_meets_min_donation(
                payload.amount,
                payload.currency,
                min_donation_amount=resolve_min_donation_for_frequency(campaign, "once"),
                default_currency=campaign.get("default_currency"),
            )

    account = resolve_nowpayments_account_for_checkout(payload.campaign_id, payload.checkout_view)
    if not account:
        raise HTTPException(
            status_code=400,
            detail="Crypto payments are not connected for this page. Attach NOWPayments in admin first.",
        )

    display_currency = payload.currency.lower()
    base_amount, total_display = _resolve_total(payload.amount, display_currency, payload.cover_fees)
    payment_ref = str(uuid.uuid4())
    frontend_url = resolve_frontend_url()

    return_url = payload.return_url or (
        f"{frontend_url}/pop-up-view?donation=success&provider=nowpayments&payment_ref={payment_ref}"
        if payload.checkout_view == "popup"
        else f"{frontend_url}/?donation=success&provider=nowpayments&payment_ref={payment_ref}"
    )
    if "payment_ref=" not in return_url:
        joiner = "&" if "?" in return_url else "?"
        return_url = f"{return_url}{joiner}payment_ref={payment_ref}"

    cancel_url = payload.cancel_url or (
        f"{frontend_url}/pop-up-view?donation=cancelled&provider=nowpayments"
        if payload.checkout_view == "popup"
        else f"{frontend_url}/?donation=cancelled&provider=nowpayments"
    )

    try:
        invoice = create_invoice(
            api_key=str(account["api_key"]),
            price_amount=total_display,
            price_currency=display_currency,
            order_id=payment_ref,
            order_description=f"Donation from {payload.donor.first_name} {payload.donor.last_name}",
            ipn_callback_url=_ipn_callback_url(),
            success_url=return_url,
            cancel_url=cancel_url,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    invoice_id = str(invoice.get("id") or "")
    return {
        "redirect_url": str(invoice["invoice_url"]),
        "payment_ref": payment_ref,
        "invoice_id": invoice_id,
        "charge_currency": display_currency.upper(),
        "charge_amount": f"{total_display:.2f}",
        "display_amount": format_display_amount(total_display, display_currency),
    }


@router.post("/complete-redirect")
def nowpayments_complete_redirect(payload: CompleteNowPaymentsRedirectRequest) -> dict[str, object]:
    account = resolve_nowpayments_account_for_checkout(payload.campaign_id, payload.checkout_view)
    if not account:
        raise HTTPException(status_code=400, detail="NOWPayments is not connected for this page.")

    display_currency = payload.currency.lower()
    base_amount, total_display = _resolve_total(payload.amount, display_currency, payload.cover_fees)
    payment_id = f"np_{payload.payment_ref}"
    if payload.invoice_id:
        payment_id = f"np_inv_{payload.invoice_id}"

    existing = rest_get_one(
        "donations",
        params={"stripe_payment_intent_id": f"eq.{payment_id}", "select": "id,status"},
    )
    if existing:
        return {
            "payment_id": payment_id,
            "status": existing.get("status") or "succeeded",
            "recorded": True,
        }

    recorded = _record_nowpayments_donation(
        payment_id=payment_id,
        payload=payload,
        base_amount=base_amount,
        total_display=total_display,
        status="succeeded",
    )
    return {
        "payment_id": payment_id,
        "status": "succeeded",
        "recorded": bool(recorded),
    }


@router.post("/ipn")
async def nowpayments_ipn(request: Request) -> dict[str, str]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid IPN body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid IPN body")

    signature = request.headers.get("x-nowpayments-sig")
    order_id = str(payload.get("order_id") or "")
    payment_status = str(payload.get("payment_status") or "").lower()

    # Prefer org account matching payment; fall back to any active account for signature check.
    accounts = rest_get(
        "nowpayments_accounts",
        params={"connection_status": "eq.active", "select": "id,ipn_secret"},
    )
    from routers.payment_accounts import _load_accounts_raw

    root_accounts = _load_accounts_raw()
    for view in ("homepage", "popup"):
        entry = root_accounts.get(view) or {}
        if entry.get("nowpayments_ipn_secret"):
            accounts.append(
                {
                    "id": f"root:{view}",
                    "ipn_secret": entry["nowpayments_ipn_secret"],
                }
            )

    verified = False
    for acct in accounts:
        secret = str(acct.get("ipn_secret") or "")
        if secret and verify_ipn_signature(payload, signature, secret):
            verified = True
            break

    if accounts and not verified:
        raise HTTPException(status_code=401, detail="Invalid IPN signature")

    if payment_status in {"finished", "confirmed"} and order_id:
        payment_id = f"np_{order_id}"
        existing = rest_get_one(
            "donations",
            params={"stripe_payment_intent_id": f"eq.{payment_id}", "select": "id,status"},
        )
        if existing and existing.get("status") != "succeeded":
            rest_patch("donations", {"status": "succeeded"}, match={"id": existing["id"]})
        # Also try invoice-keyed rows
        invoice_id = payload.get("invoice_id")
        if invoice_id:
            inv_key = f"np_inv_{invoice_id}"
            inv_row = rest_get_one(
                "donations",
                params={"stripe_payment_intent_id": f"eq.{inv_key}", "select": "id,status"},
            )
            if inv_row and inv_row.get("status") != "succeeded":
                rest_patch("donations", {"status": "succeeded"}, match={"id": inv_row["id"]})

    return {"status": "ok"}
