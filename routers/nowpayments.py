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


def _checkout_payload_from_prepare(payload: PrepareNowPaymentsRedirectRequest) -> dict[str, Any]:
    return payload.model_dump(mode="json")


def _save_pending_checkout(
    *,
    payment_ref: str,
    invoice_id: str | None,
    payload: PrepareNowPaymentsRedirectRequest,
) -> None:
    rest_insert(
        "nowpayments_checkouts",
        {
            "payment_ref": payment_ref,
            "invoice_id": invoice_id or None,
            "payload": _checkout_payload_from_prepare(payload),
        },
        on_conflict="payment_ref",
    )


def _load_pending_checkout(
    *,
    payment_ref: str | None = None,
    invoice_id: str | None = None,
) -> dict[str, Any] | None:
    if payment_ref:
        row = rest_get_one(
            "nowpayments_checkouts",
            params={"payment_ref": f"eq.{payment_ref}", "select": "*"},
        )
        if row:
            return row
    if invoice_id:
        return rest_get_one(
            "nowpayments_checkouts",
            params={"invoice_id": f"eq.{invoice_id}", "select": "*"},
        )
    return None


def _complete_request_from_pending(
    pending: dict[str, Any],
    *,
    payment_ref: str | None = None,
    invoice_id: str | None = None,
) -> CompleteNowPaymentsRedirectRequest:
    raw = pending.get("payload") or {}
    if isinstance(raw, str):
        import json

        raw = json.loads(raw)
    data = dict(raw)
    data["payment_ref"] = payment_ref or pending.get("payment_ref") or data.get("payment_ref")
    data["invoice_id"] = invoice_id or pending.get("invoice_id") or data.get("invoice_id")
    return CompleteNowPaymentsRedirectRequest.model_validate(data)


def _find_donation_by_nowpayments_keys(
    *,
    payment_ref: str | None = None,
    invoice_id: str | None = None,
) -> dict[str, Any] | None:
    keys: list[str] = []
    if payment_ref:
        keys.append(f"np_{payment_ref}")
    if invoice_id:
        keys.append(f"np_inv_{invoice_id}")
    for key in keys:
        row = rest_get_one(
            "donations",
            params={"stripe_payment_intent_id": f"eq.{key}", "select": "id,status,stripe_payment_intent_id"},
        )
        if row:
            return row
    return None


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
            from routers.payment_accounts import resolve_root_nowpayments_account, uses_platform_provider

            if uses_platform_provider(str(org_id), "nowpayments", str(campaign_id)):
                return pick(resolve_root_nowpayments_account("homepage"))

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
    crypto_amount: float | None = None,
    crypto_currency: str | None = None,
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
    if crypto_amount is not None:
        row["crypto_amount"] = crypto_amount
    if crypto_currency:
        row["crypto_currency"] = crypto_currency.upper()
    device = {}
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
    checkout_view = getattr(payload, "checkout_view", None)
    device["checkout_view"] = checkout_view if checkout_view in ("homepage", "popup") else "homepage"
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
    from currency import convert_for_nowpayments

    invoice_currency, invoice_amount = convert_for_nowpayments(total_display, display_currency)
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
            price_amount=invoice_amount,
            price_currency=invoice_currency.lower(),
            order_id=payment_ref,
            order_description=f"Donation from {payload.donor.first_name} {payload.donor.last_name}",
            ipn_callback_url=_ipn_callback_url(),
            success_url=return_url,
            cancel_url=cancel_url,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    invoice_id = str(invoice.get("id") or "")
    _save_pending_checkout(payment_ref=payment_ref, invoice_id=invoice_id or None, payload=payload)
    return {
        "redirect_url": str(invoice["invoice_url"]),
        "payment_ref": payment_ref,
        "invoice_id": invoice_id,
        "charge_currency": invoice_currency.upper(),
        "charge_amount": f"{invoice_amount:.2f}",
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

    existing = _find_donation_by_nowpayments_keys(
        payment_ref=payload.payment_ref,
        invoice_id=payload.invoice_id,
    )
    if existing:
        return {
            "payment_id": existing.get("stripe_payment_intent_id") or payment_id,
            "status": existing.get("status") or "succeeded",
            "recorded": True,
        }

    _save_pending_checkout(
        payment_ref=payload.payment_ref,
        invoice_id=payload.invoice_id,
        payload=payload,
    )

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


class CompleteByRefRequest(BaseModel):
    payment_ref: str = Field(min_length=5, max_length=128)
    invoice_id: str | None = None


@router.post("/complete-by-ref")
def nowpayments_complete_by_ref(payload: CompleteByRefRequest) -> dict[str, object]:
    """Record donation using server-stored checkout when browser sessionStorage is missing."""
    pending = _load_pending_checkout(payment_ref=payload.payment_ref, invoice_id=payload.invoice_id)
    if not pending:
        raise HTTPException(status_code=404, detail="Checkout session not found for this payment.")

    complete = _complete_request_from_pending(
        pending,
        payment_ref=payload.payment_ref,
        invoice_id=payload.invoice_id,
    )
    return nowpayments_complete_redirect(complete)


def _crypto_fields_from_ipn(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract paid crypto amount/ticker from NOWPayments IPN body."""
    amount = payload.get("actually_paid")
    if amount is None:
        amount = payload.get("pay_amount")
    currency = payload.get("pay_currency") or payload.get("outcome_currency")
    out: dict[str, Any] = {}
    try:
        if amount is not None and str(amount).strip() != "":
            out["crypto_amount"] = float(amount)
    except (TypeError, ValueError):
        pass
    if currency:
        out["crypto_currency"] = str(currency).upper()
    return out


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
    invoice_id = str(payload.get("invoice_id") or "") or None

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

    if payment_status in {"finished", "confirmed", "partially_paid"} and (order_id or invoice_id):
        # Treat partial as succeeded: donor paid (fees/underpay may short the invoice),
        # and we still store the crypto that actually landed.
        crypto_fields = _crypto_fields_from_ipn(payload)
        status = "succeeded"
        updates = {"status": status, **crypto_fields}
        if payment_status == "partially_paid":
            updates["comment"] = "NOWPayments partial payment accepted as succeeded"

        existing = _find_donation_by_nowpayments_keys(payment_ref=order_id or None, invoice_id=invoice_id)
        if existing:
            rest_patch("donations", updates, match={"id": existing["id"]})
            return {"status": "ok"}

        # Donor may never have returned to success URL — create from stored checkout.
        pending = _load_pending_checkout(payment_ref=order_id or None, invoice_id=invoice_id)
        if pending:
            complete = _complete_request_from_pending(
                pending,
                payment_ref=order_id or None,
                invoice_id=invoice_id,
            )
            display_currency = complete.currency.lower()
            base_amount, total_display = _resolve_total(
                complete.amount, display_currency, complete.cover_fees
            )
            payment_id = f"np_{complete.payment_ref}"
            if complete.invoice_id:
                payment_id = f"np_inv_{complete.invoice_id}"
            _record_nowpayments_donation(
                payment_id=payment_id,
                payload=complete,
                base_amount=base_amount,
                total_display=total_display,
                status=status,
                crypto_amount=crypto_fields.get("crypto_amount"),
                crypto_currency=crypto_fields.get("crypto_currency"),
            )

    return {"status": "ok"}
