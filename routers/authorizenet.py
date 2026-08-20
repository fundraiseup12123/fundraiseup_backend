"""Authorize.net checkout + org credential attach (hybrid gateway with PayPal Google Pay)."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import AuthUser, deny_platform_admin_payment_writes, require_auth, require_org_access
from authorizenet_client import (
    acceptjs_script_url,
    authenticate_test,
    complete_paypal_express,
    create_arb_subscription_from_opaque,
    create_paypal_express,
    create_transaction_opaque,
    credential_hint,
)
from currency import calculate_total_with_fees, convert_to_reporting, estimate_processing_fee
from db import rest_delete, rest_get, rest_get_one, rest_insert, rest_patch
from frontend_url import resolve_frontend_url
from supabase_client import insert_donation, supabase_enabled

router = APIRouter(prefix="/authorizenet", tags=["authorizenet"])
logger = logging.getLogger(__name__)


def _processor_is_hybrid(campaign_id: str | None) -> bool:
    from routers.payment_accounts import resolve_payment_processor

    return resolve_payment_processor(None, campaign_id) == "authorizenet_paypal"


def _require_hybrid(campaign_id: str | None) -> None:
    if not _processor_is_hybrid(campaign_id):
        raise HTTPException(
            status_code=400,
            detail="Authorize.net + PayPal processor is not enabled for this campaign",
        )


def _public_account(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "organization_id": row.get("organization_id"),
        "campaign_id": row.get("campaign_id"),
        "api_login_id_hint": row.get("api_login_id_hint")
        or credential_hint(str(row.get("api_login_id") or "")),
        "public_client_key_hint": row.get("public_client_key_hint")
        or credential_hint(str(row.get("public_client_key") or "")),
        "env": row.get("env") or "production",
        "is_default": bool(row.get("is_default")),
        "connection_status": row.get("connection_status") or "pending",
        "has_keys": bool(row.get("api_login_id") and row.get("transaction_key") and row.get("public_client_key")),
    }


def _clear_org_defaults(org_id: str) -> None:
    defaults = rest_get(
        "authorizenet_accounts",
        params={
            "organization_id": f"eq.{org_id}",
            "is_default": "eq.true",
            "select": "id",
        },
    )
    for row in defaults:
        rest_patch("authorizenet_accounts", {"is_default": False}, match={"id": row["id"]})


def resolve_authorizenet_credentials_for_checkout(
    campaign_id: str | None,
    checkout_view: Literal["homepage", "popup", "landing"] = "homepage",
) -> dict[str, str] | None:
    """Platform homepage/popup/landing keys or org default authorizenet_accounts row."""
    from routers.payment_accounts import (
        get_platform_authorizenet_credentials,
        normalize_payment_view,
        resolve_payment_account_sources,
        uses_platform_provider,
    )
    from site_constants import ROOT_CAMPAIGN_ID, ROOT_ORG_ID

    view = normalize_payment_view(checkout_view)
    org_id: str | None = None
    if campaign_id:
        campaign = rest_get_one(
            "campaigns",
            params={
                "id": f"eq.{campaign_id}",
                "select": "organization_id,authorizenet_account_id",
            },
        )
        if campaign:
            org_id = str(campaign.get("organization_id") or "") or None
            acct_id = campaign.get("authorizenet_account_id")
            if (
                view != "landing"
                and acct_id
                and not uses_platform_provider(org_id, "authorizenet", campaign_id)
            ):
                row = rest_get_one(
                    "authorizenet_accounts",
                    params={"id": f"eq.{acct_id}", "select": "*"},
                )
                if row and row.get("api_login_id") and row.get("transaction_key"):
                    return {
                        "api_login_id": str(row["api_login_id"]),
                        "transaction_key": str(row["transaction_key"]),
                        "signature_key": str(row.get("signature_key") or ""),
                        "public_client_key": str(row.get("public_client_key") or ""),
                        "env": str(row.get("env") or "production"),
                        "keys_source": "organization",
                    }

    if not org_id and campaign_id == ROOT_CAMPAIGN_ID:
        org_id = ROOT_ORG_ID

    sources = resolve_payment_account_sources(org_id, campaign_id)
    if view == "landing" or str(sources.get("authorizenet") or "").lower() == "platform" or not org_id:
        platform = get_platform_authorizenet_credentials(view)
        if platform:
            return platform
        if view != "homepage":
            return get_platform_authorizenet_credentials("homepage")
        return None

    rows = rest_get(
        "authorizenet_accounts",
        params={
            "organization_id": f"eq.{org_id}",
            "is_default": "eq.true",
            "select": "*",
            "limit": "1",
        },
    )
    row = rows[0] if rows else None
    if not row:
        rows = rest_get(
            "authorizenet_accounts",
            params={"organization_id": f"eq.{org_id}", "select": "*", "limit": "1"},
        )
        row = rows[0] if rows else None
    if not row:
        # Fall back to platform if org has no keys
        return get_platform_authorizenet_credentials(view) or get_platform_authorizenet_credentials(
            "homepage"
        )
    return {
        "api_login_id": str(row.get("api_login_id") or ""),
        "transaction_key": str(row.get("transaction_key") or ""),
        "signature_key": str(row.get("signature_key") or ""),
        "public_client_key": str(row.get("public_client_key") or ""),
        "env": str(row.get("env") or "production"),
        "keys_source": "organization",
    }


class AttachAuthorizeNetKeysRequest(BaseModel):
    api_login_id: str = Field(min_length=2, max_length=64)
    transaction_key: str = Field(min_length=8, max_length=128)
    public_client_key: str = Field(min_length=8, max_length=512)
    signature_key: str | None = Field(default=None, max_length=512)
    env: Literal["sandbox", "production"] = "production"
    campaign_id: str | None = None
    is_default: bool = True


@router.get("/orgs/{org_id}/accounts")
def list_authorizenet_accounts(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> list[dict[str, Any]]:
    require_org_access(org_id, user, min_role="member")
    rows = rest_get("authorizenet_accounts", params={"organization_id": f"eq.{org_id}", "select": "*"})
    return [_public_account(row) for row in rows]


@router.post("/orgs/{org_id}/accounts")
def attach_authorizenet_keys(
    org_id: str,
    payload: AttachAuthorizeNetKeysRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    deny_platform_admin_payment_writes(user)

    login = payload.api_login_id.strip()
    txn_key = payload.transaction_key.strip()
    public_key = payload.public_client_key.strip()
    signature = (payload.signature_key or "").strip() or None
    env = payload.env
    if not authenticate_test(login, txn_key, env=env):
        raise HTTPException(
            status_code=400,
            detail=(
                "Authorize.net API Login ID / Transaction Key are invalid for the selected "
                f"environment ({env}), or the API is unreachable."
            ),
        )

    is_default = bool(payload.is_default) and not payload.campaign_id
    if is_default:
        _clear_org_defaults(org_id)

    existing = rest_get_one(
        "authorizenet_accounts",
        params={
            "organization_id": f"eq.{org_id}",
            "api_login_id": f"eq.{login}",
            "select": "id",
        },
    )
    row_data = {
        "organization_id": org_id,
        "campaign_id": payload.campaign_id,
        "api_login_id": login,
        "transaction_key": txn_key,
        "signature_key": signature,
        "public_client_key": public_key,
        "api_login_id_hint": credential_hint(login),
        "public_client_key_hint": credential_hint(public_key),
        "env": env,
        "is_default": is_default,
        "connection_status": "active",
    }
    if existing and existing.get("id"):
        row = rest_patch("authorizenet_accounts", row_data, match={"id": existing["id"]}) or {
            **row_data,
            "id": existing["id"],
        }
    else:
        row = rest_insert("authorizenet_accounts", row_data)
    if not row:
        raise HTTPException(
            status_code=500,
            detail="Unable to save Authorize.net account. Run backend/sql/037_authorizenet_paypal.sql in Supabase.",
        )

    if payload.campaign_id:
        rest_patch(
            "campaigns",
            {"authorizenet_account_id": row["id"]},
            match={"id": payload.campaign_id},
        )

    return _public_account(row)


@router.delete("/orgs/{org_id}/accounts/{account_id}")
def remove_authorizenet_account(
    org_id: str,
    account_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, bool]:
    require_org_access(org_id, user, min_role="admin")
    deny_platform_admin_payment_writes(user)
    row = rest_get_one(
        "authorizenet_accounts",
        params={"id": f"eq.{account_id}", "organization_id": f"eq.{org_id}", "select": "id"},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Authorize.net account not found")
    rest_delete("authorizenet_accounts", match={"id": account_id})
    return {"removed": True}


class AnetDonor(BaseModel):
    first_name: str = Field(default="", max_length=80)
    last_name: str = Field(default="", max_length=80)
    email: str = Field(default="", max_length=254)


class AnetUtm(BaseModel):
    source: str | None = None
    medium: str | None = None
    campaign: str | None = None
    term: str | None = None
    content: str | None = None


class AnetDevice(BaseModel):
    os: str | None = None
    browser: str | None = None
    type: str | None = None
    country: str | None = None
    city: str | None = None
    gender: str | None = None


class AnetChargeRequest(BaseModel):
    amount: float = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    frequency: Literal["once", "monthly"] = "once"
    cover_fees: bool = False
    dedicate: bool = False
    honoree_name: str | None = None
    comment: str | None = None
    campaign_id: str | None = None
    checkout_view: Literal["homepage", "popup", "landing"] = "homepage"
    donor: AnetDonor
    utm: AnetUtm | None = None
    device: AnetDevice | None = None
    payment_method: Literal["card", "apple_pay", "paypal"] = "card"
    data_descriptor: str = Field(min_length=3, max_length=128)
    data_value: str = Field(min_length=8, max_length=10000)


class AnetSubscribeRequest(AnetChargeRequest):
    frequency: Literal["monthly"] = "monthly"
    payment_method: Literal["card", "apple_pay"] = "card"


class AnetPayPalPrepareRequest(BaseModel):
    amount: float = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    frequency: Literal["once", "monthly"] = "once"
    cover_fees: bool = False
    dedicate: bool = False
    honoree_name: str | None = None
    comment: str | None = None
    campaign_id: str | None = None
    checkout_view: Literal["homepage", "popup", "landing"] = "homepage"
    donor: AnetDonor
    utm: AnetUtm | None = None
    device: AnetDevice | None = None
    return_url: str | None = None
    cancel_url: str | None = None


class AnetPayPalCompleteRequest(BaseModel):
    payment_ref: str = Field(min_length=4, max_length=128)
    payer_id: str = Field(min_length=1, max_length=128)
    ref_trans_id: str | None = None
    amount: float = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    frequency: Literal["once", "monthly"] = "once"
    cover_fees: bool = False
    dedicate: bool = False
    honoree_name: str | None = None
    comment: str | None = None
    campaign_id: str | None = None
    checkout_view: Literal["homepage", "popup", "landing"] = "homepage"
    donor: AnetDonor
    utm: AnetUtm | None = None
    device: AnetDevice | None = None


def _resolve_total(amount: float, currency: str, cover_fees: bool) -> tuple[float, float]:
    base = round(float(amount), 2)
    total_display = calculate_total_with_fees(base, currency) if cover_fees else base
    return base, round(float(total_display), 2)


def _charge_amount_and_currency(total_display: float, display_currency: str) -> tuple[float, str]:
    """Authorize.net merchant accounts in this product are USD — convert when needed."""
    code = (display_currency or "USD").upper()
    if code == "USD":
        return round(float(total_display), 2), "USD"
    try:
        converted = convert_to_reporting(total_display, code, "USD")
        return round(float(converted), 2), "USD"
    except Exception:
        return round(float(total_display), 2), code


def _resolve_org_id(campaign_id: str | None) -> str:
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


def _record_anet_donation(
    *,
    order_id: str,
    payment_method: str,
    donor: AnetDonor,
    amount: float,
    currency: str,
    frequency: str,
    cover_fees: bool,
    campaign_id: str | None,
    checkout_view: str,
    honoree_name: str | None,
    comment: str | None,
    utm: AnetUtm | None,
    device: AnetDevice | None,
    status: str = "succeeded",
) -> dict[str, Any] | None:
    base_amount, total_display = _resolve_total(amount, currency, cover_fees)
    if cover_fees:
        processing_fee = max(0.0, round(total_display - base_amount, 2))
        payout_amount = base_amount
    else:
        processing_fee = estimate_processing_fee(base_amount, currency.upper())
        payout_amount = max(0.0, round(base_amount - processing_fee, 2))

    from site_constants import ROOT_CAMPAIGN_ID

    cid = campaign_id or ROOT_CAMPAIGN_ID
    donation_status = status if status in {"succeeded", "failed", "pending"} else "succeeded"
    first_name = (donor.first_name or "").strip()
    last_name = (donor.last_name or "").strip()
    email = (donor.email or "").strip()
    if first_name.lower() in {"", "donor", "guest", "anonymous"} and last_name.lower() in {
        "",
        "donor",
        "guest",
        "anonymous",
    }:
        first_name, last_name = "Anonymous", ""
    if email.lower() in {"", "pending@wallet.local", "donor@example.com"}:
        email = ""
    row: dict[str, Any] = {
        "stripe_payment_intent_id": order_id,
        "first_name": first_name or "Anonymous",
        "last_name": last_name,
        "email": email or None,
        "amount": total_display,
        "base_amount": base_amount,
        "currency": currency.upper(),
        "frequency": frequency,
        "payment_method": payment_method,
        "payment_processor": "authorizenet_paypal",
        "honoree_name": honoree_name or None,
        "comment": comment or None,
        "organization_id": _resolve_org_id(cid),
        "campaign_id": cid,
        "status": donation_status,
        "fee_covered": cover_fees,
        "platform_fee": 0,
        "processing_fee": processing_fee,
        "payout_amount": payout_amount if donation_status == "succeeded" else 0,
    }
    device_data: dict[str, Any] = {}
    if device:
        device_data = {
            k: v
            for k, v in {
                "os": device.os,
                "browser": device.browser,
                "type": device.type,
                "country": device.country,
                "city": device.city,
                "gender": device.gender,
            }.items()
            if v
        }
    device_data["checkout_view"] = checkout_view if checkout_view in ("homepage", "popup", "landing") else "homepage"
    device_data["processor"] = "authorizenet"
    row["device"] = device_data
    if utm:
        utm_data = {
            k: v
            for k, v in {
                "source": utm.source,
                "medium": utm.medium,
                "campaign": utm.campaign,
                "term": utm.term,
                "content": utm.content,
            }.items()
            if v
        }
        if utm_data:
            row["utm"] = utm_data

    if not supabase_enabled():
        return None
    return insert_donation(row)


def _send_emails(saved: dict[str, Any] | None) -> None:
    if not saved:
        return
    if str(saved.get("status") or "").lower() != "succeeded":
        return
    try:
        from emails import send_donation_alerts_for_row, send_donation_confirmation_for_row

        send_donation_confirmation_for_row(saved)
        send_donation_alerts_for_row(saved)
    except Exception:
        logger.exception("Post-donation emails failed for Authorize.net donation %s", saved.get("id"))


def _record_failed_anet_charge(
    payload: AnetChargeRequest,
    *,
    detail: str,
    payment_method: str,
    frequency: str,
) -> None:
    """Persist declined/failed attempts so admin lists show Failed instead of nothing/Succeeded."""
    try:
        fail_id = f"authorizenet:failed:{uuid.uuid4().hex[:16]}"
        comment = (payload.comment or "").strip()
        fail_note = (detail or "Card was declined")[:400]
        _record_anet_donation(
            order_id=fail_id,
            payment_method=payment_method,
            donor=payload.donor,
            amount=payload.amount,
            currency=payload.currency,
            frequency=frequency,
            cover_fees=payload.cover_fees,
            campaign_id=payload.campaign_id,
            checkout_view=payload.checkout_view,
            honoree_name=payload.honoree_name,
            comment=f"{comment + ' · ' if comment else ''}Payment failed: {fail_note}"[:500],
            utm=payload.utm,
            device=payload.device,
            status="failed",
        )
    except Exception:
        logger.exception("Unable to record failed Authorize.net donation")


@router.get("/checkout-config")
def authorizenet_checkout_config(
    campaign_id: str | None = Query(None),
    checkout_view: Literal["homepage", "popup", "landing"] = Query("homepage"),
) -> dict[str, Any]:
    from routers.payment_accounts import resolve_payment_processor

    processor = resolve_payment_processor(None, campaign_id)
    creds = resolve_authorizenet_credentials_for_checkout(campaign_id, checkout_view)
    available = processor in {"authorizenet_paypal", "paypal"} and bool(
        creds
        and creds.get("api_login_id")
        and creds.get("public_client_key")
        and creds.get("transaction_key")
    )
    env = str((creds or {}).get("env") or "production")
    return {
        "available": available,
        "payment_processor": processor,
        "api_login_id": str((creds or {}).get("api_login_id") or "") if available else "",
        "public_client_key": str((creds or {}).get("public_client_key") or "") if available else "",
        "env": env if available else "production",
        "acceptjs_url": acceptjs_script_url(env) if available else "",
        "keys_source": str((creds or {}).get("keys_source") or "") if available else "",
    }


@router.post("/charge")
def authorizenet_charge(payload: AnetChargeRequest) -> dict[str, Any]:
    _require_hybrid(payload.campaign_id)
    creds = resolve_authorizenet_credentials_for_checkout(payload.campaign_id, payload.checkout_view)
    if not creds:
        raise HTTPException(status_code=400, detail="Authorize.net credentials are not configured")

    base_amount, total_display = _resolve_total(payload.amount, payload.currency, payload.cover_fees)
    charge_amount, charge_currency = _charge_amount_and_currency(total_display, payload.currency)
    invoice = f"d{int(time.time())}{uuid.uuid4().hex[:6]}"[:20]
    try:
        result = create_transaction_opaque(
            api_login_id=creds["api_login_id"],
            transaction_key=creds["transaction_key"],
            amount=charge_amount,
            currency=charge_currency,
            data_descriptor=payload.data_descriptor.strip(),
            data_value=payload.data_value.strip(),
            order_invoice=invoice,
            customer_email=payload.donor.email,
            env=creds.get("env") or "production",
        )
    except RuntimeError as exc:
        _record_failed_anet_charge(
            payload,
            detail=str(exc),
            payment_method=payload.payment_method,
            frequency="once",
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    txn_id = result.get("transaction_id") or invoice
    order_id = f"authorizenet:{txn_id}"
    saved = _record_anet_donation(
        order_id=order_id,
        payment_method=payload.payment_method,
        donor=payload.donor,
        amount=payload.amount,
        currency=payload.currency,
        frequency=payload.frequency,
        cover_fees=payload.cover_fees,
        campaign_id=payload.campaign_id,
        checkout_view=payload.checkout_view,
        honoree_name=payload.honoree_name,
        comment=payload.comment,
        utm=payload.utm,
        device=payload.device,
        status="succeeded",
    )
    _send_emails(saved)
    return {
        "order_id": order_id,
        "transaction_id": txn_id,
        "status": "COMPLETED",
        "recorded": bool(saved),
    }


@router.post("/subscribe")
def authorizenet_subscribe(payload: AnetSubscribeRequest) -> dict[str, Any]:
    _require_hybrid(payload.campaign_id)
    email = (payload.donor.email or "").strip().lower()
    if not email or email in {"pending@wallet.local", "donor@example.com"}:
        raise HTTPException(
            status_code=400,
            detail="Enter a valid email address before starting a monthly donation.",
        )
    creds = resolve_authorizenet_credentials_for_checkout(payload.campaign_id, payload.checkout_view)
    if not creds:
        raise HTTPException(status_code=400, detail="Authorize.net credentials are not configured")

    base_amount, total_display = _resolve_total(payload.amount, payload.currency, payload.cover_fees)
    charge_amount, charge_currency = _charge_amount_and_currency(total_display, payload.currency)
    try:
        result = create_arb_subscription_from_opaque(
            api_login_id=creds["api_login_id"],
            transaction_key=creds["transaction_key"],
            amount=charge_amount,
            currency=charge_currency,
            data_descriptor=payload.data_descriptor.strip(),
            data_value=payload.data_value.strip(),
            customer_email=payload.donor.email,
            first_name=payload.donor.first_name,
            last_name=payload.donor.last_name,
            subscription_name="Monthly donation",
            env=creds.get("env") or "production",
        )
    except RuntimeError as exc:
        _record_failed_anet_charge(
            payload,
            detail=str(exc),
            payment_method=payload.payment_method,
            frequency="monthly",
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sub_id = result["subscription_id"]
    txn_id = str(result.get("transaction_id") or "").strip()
    order_id = f"authorizenet:sub:{sub_id}"
    if txn_id:
        # Keep a stable link to the first-month charge as well.
        order_id = f"authorizenet:sub:{sub_id}:txn:{txn_id}"
    saved = _record_anet_donation(
        order_id=order_id,
        payment_method=payload.payment_method,
        donor=payload.donor,
        amount=payload.amount,
        currency=payload.currency,
        frequency="monthly",
        cover_fees=payload.cover_fees,
        campaign_id=payload.campaign_id,
        checkout_view=payload.checkout_view,
        honoree_name=payload.honoree_name,
        comment=payload.comment,
        utm=payload.utm,
        device=payload.device,
        status="succeeded",
    )
    _send_emails(saved)
    return {
        "subscription_id": sub_id,
        "order_id": order_id,
        "status": "ACTIVE",
        "recorded": bool(saved),
        "transaction_id": txn_id or None,
    }


# Pending PayPal Express metadata (in-memory; fine for redirect round-trip)
_paypal_pending: dict[str, dict[str, Any]] = {}


@router.post("/paypal/prepare")
def authorizenet_paypal_prepare(payload: AnetPayPalPrepareRequest) -> dict[str, Any]:
    _require_hybrid(payload.campaign_id)
    creds = resolve_authorizenet_credentials_for_checkout(payload.campaign_id, payload.checkout_view)
    if not creds:
        raise HTTPException(status_code=400, detail="Authorize.net credentials are not configured")

    base_amount, total_display = _resolve_total(payload.amount, payload.currency, payload.cover_fees)
    frontend = resolve_frontend_url()
    payment_ref = f"anetpp_{uuid.uuid4().hex[:20]}"
    return_url = payload.return_url or (
        f"{frontend}/?donation=success&provider=authorizenet&payment_ref={payment_ref}"
    )
    cancel_url = payload.cancel_url or f"{frontend}/?donation=cancelled&provider=authorizenet"

    try:
        result = create_paypal_express(
            api_login_id=creds["api_login_id"],
            transaction_key=creds["transaction_key"],
            amount=total_display,
            currency=payload.currency,
            success_url=return_url,
            cancel_url=cancel_url,
            order_invoice=payment_ref[:20],
            customer_email=payload.donor.email,
            env=creds.get("env") or "production",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    redirect_url = str(result.get("redirect_url") or "").strip()
    if not redirect_url:
        raise HTTPException(
            status_code=400,
            detail="Authorize.net did not return a PayPal redirect URL. Ensure PayPal is enabled on the merchant account.",
        )

    _paypal_pending[payment_ref] = {
        "payload": payload.model_dump(),
        "ref_trans_id": result.get("transaction_id"),
        "total_display": total_display,
        "base_amount": base_amount,
    }
    return {
        "payment_ref": payment_ref,
        "redirect_url": redirect_url,
        "ref_trans_id": result.get("transaction_id"),
    }


@router.post("/paypal/complete")
def authorizenet_paypal_complete(payload: AnetPayPalCompleteRequest) -> dict[str, Any]:
    _require_hybrid(payload.campaign_id)
    creds = resolve_authorizenet_credentials_for_checkout(payload.campaign_id, payload.checkout_view)
    if not creds:
        raise HTTPException(status_code=400, detail="Authorize.net credentials are not configured")

    pending = _paypal_pending.pop(payload.payment_ref, None)
    base_amount, total_display = _resolve_total(payload.amount, payload.currency, payload.cover_fees)
    if pending:
        total_display = float(pending.get("total_display") or total_display)
        base_amount = float(pending.get("base_amount") or base_amount)

    ref_trans_id = payload.ref_trans_id or (pending or {}).get("ref_trans_id")
    try:
        result = complete_paypal_express(
            api_login_id=creds["api_login_id"],
            transaction_key=creds["transaction_key"],
            amount=total_display,
            currency=payload.currency,
            payer_id=payload.payer_id.strip(),
            ref_trans_id=str(ref_trans_id) if ref_trans_id else None,
            env=creds.get("env") or "production",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    txn_id = result.get("transaction_id") or payload.payment_ref
    order_id = f"authorizenet:paypal:{txn_id}"
    saved = _record_anet_donation(
        order_id=order_id,
        payment_method="paypal",
        donor=payload.donor,
        amount=payload.amount,
        currency=payload.currency,
        frequency=payload.frequency,
        cover_fees=payload.cover_fees,
        campaign_id=payload.campaign_id,
        checkout_view=payload.checkout_view,
        honoree_name=payload.honoree_name,
        comment=payload.comment,
        utm=payload.utm,
        device=payload.device,
    )
    _send_emails(saved)
    return {
        "order_id": order_id,
        "transaction_id": txn_id,
        "status": "COMPLETED",
        "recorded": bool(saved),
        "base_amount": base_amount,
    }
