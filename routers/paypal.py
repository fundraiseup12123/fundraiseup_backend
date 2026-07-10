from __future__ import annotations

import json
import os
import uuid
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from frontend_url import resolve_frontend_url
from currency import (
    calculate_total_with_fees,
    convert_for_paypal,
    estimate_processing_fee,
    format_display_amount,
    paypal_checkout_currency,
)
from paypal_client import capture_paypal_order, create_paypal_order, paypal_configured, paypal_env
from supabase_client import insert_donation, supabase_enabled

router = APIRouter(prefix="/paypal", tags=["paypal"])


class PayPalDonor(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=254)


class PayPalUtm(BaseModel):
    source: str | None = None
    medium: str | None = None
    campaign: str | None = None
    term: str | None = None
    content: str | None = None


class PayPalDevice(BaseModel):
    os: str | None = None
    browser: str | None = None
    type: str | None = None
    country: str | None = None
    city: str | None = None
    gender: str | None = None


class CreatePayPalOrderRequest(BaseModel):
    amount: float = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    frequency: Literal["once", "monthly"] = "once"
    cover_fees: bool = False
    dedicate: bool = False
    honoree_name: str | None = None
    comment: str | None = None
    campaign_id: str | None = None
    checkout_view: Literal["homepage", "popup"] = "homepage"
    donor: PayPalDonor
    utm: PayPalUtm | None = None
    device: PayPalDevice | None = None
    return_url: str | None = None
    cancel_url: str | None = None


class PreparePayPalRedirectRequest(CreatePayPalOrderRequest):
    pass


class CompletePayPalRedirectRequest(BaseModel):
    payment_ref: str = Field(min_length=8, max_length=64)
    amount: float = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    frequency: Literal["once", "monthly"] = "once"
    cover_fees: bool = False
    dedicate: bool = False
    honoree_name: str | None = None
    comment: str | None = None
    campaign_id: str | None = None
    checkout_view: Literal["homepage", "popup"] = "homepage"
    donor: PayPalDonor
    utm: PayPalUtm | None = None
    device: PayPalDevice | None = None
    paypal_txn_id: str | None = None


class CreatePayPalOrderResponse(BaseModel):
    order_id: str
    charge_currency: str
    charge_amount: float
    display_amount: str
    conversion_note: str | None = None


class CapturePayPalOrderRequest(BaseModel):
    order_id: str = Field(min_length=3, max_length=64)
    amount: float = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    frequency: Literal["once", "monthly"] = "once"
    cover_fees: bool = False
    dedicate: bool = False
    honoree_name: str | None = None
    comment: str | None = None
    campaign_id: str | None = None
    donor: PayPalDonor
    utm: PayPalUtm | None = None
    device: PayPalDevice | None = None


class CapturePayPalOrderResponse(BaseModel):
    order_id: str
    status: str
    recorded: bool


def _resolve_total(amount: float, currency: str, cover_fees: bool) -> tuple[float, float]:
    base_amount = amount
    total_display = calculate_total_with_fees(amount, currency) if cover_fees else amount
    return base_amount, total_display


def _metadata_payload(payload: CreatePayPalOrderRequest | CapturePayPalOrderRequest, base_amount: float) -> dict:
    meta = {
        "first_name": payload.donor.first_name,
        "last_name": payload.donor.last_name,
        "email": payload.donor.email,
        "frequency": payload.frequency,
        "dedicate": str(payload.dedicate).lower(),
        "honoree_name": (payload.honoree_name or "")[:500],
        "comment": (payload.comment or "")[:500],
        "base_amount": str(base_amount),
        "cover_fees": str(payload.cover_fees).lower(),
        "display_currency": payload.currency.upper(),
        "payment_method": "paypal",
    }
    if payload.campaign_id:
        meta["campaign_id"] = payload.campaign_id
    if payload.utm:
        utm_fields = {
            "utm_source": payload.utm.source,
            "utm_medium": payload.utm.medium,
            "utm_campaign": payload.utm.campaign,
            "utm_term": payload.utm.term,
            "utm_content": payload.utm.content,
        }
        for key, value in utm_fields.items():
            if value:
                meta[key] = value[:500]
    return meta


def _paypal_webscr_base() -> str:
    if paypal_env() == "live":
        return "https://www.paypal.com/cgi-bin/webscr"
    return "https://www.sandbox.paypal.com/cgi-bin/webscr"


def _resolve_paypal_organization_id(campaign_id: str | None) -> str:
    from db import rest_get_one
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


def _record_paypal_donation(
    *,
    order_id: str,
    payload: CreatePayPalOrderRequest | CompletePayPalRedirectRequest,
    base_amount: float,
    total_display: float,
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

    row = {
        "stripe_payment_intent_id": order_id,
        "first_name": payload.donor.first_name,
        "last_name": payload.donor.last_name,
        "email": payload.donor.email,
        "amount": total_display,
        "base_amount": base_amount,
        "currency": display_currency,
        "frequency": payload.frequency,
        "payment_method": "paypal",
        "honoree_name": payload.honoree_name or None,
        "comment": payload.comment or None,
        "organization_id": _resolve_paypal_organization_id(campaign_id),
        "campaign_id": campaign_id,
        "status": "succeeded",
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


@router.get("/checkout-config")
def paypal_checkout_config(
    campaign_id: str | None = Query(None),
    checkout_view: Literal["homepage", "popup"] = Query("homepage"),
) -> dict[str, object]:
    from routers.paypal_connect import resolve_paypal_payee_email_for_checkout

    payee = resolve_paypal_payee_email_for_checkout(campaign_id, checkout_view)
    return {
        "available": bool(payee),
        "merchant_connected": bool(payee),
        "mode": "redirect" if payee else "unavailable",
        "currency": paypal_checkout_currency(),
        "api_configured": paypal_configured(),
    }


@router.post("/prepare-redirect")
def paypal_prepare_redirect(payload: PreparePayPalRedirectRequest) -> dict[str, str]:
    from routers.paypal_connect import resolve_paypal_payee_email_for_checkout

    if payload.frequency != "once":
        raise HTTPException(status_code=400, detail="PayPal is only available for one-time donations")

    payee = resolve_paypal_payee_email_for_checkout(payload.campaign_id, payload.checkout_view)
    if not payee:
        raise HTTPException(
            status_code=400,
            detail="PayPal is not connected for this page. Connect a PayPal account in admin first.",
        )

    display_currency = payload.currency.lower()
    base_amount, total_display = _resolve_total(payload.amount, display_currency, payload.cover_fees)
    charge_currency_code, charge_amount = convert_for_paypal(total_display, display_currency)
    frontend_url = resolve_frontend_url()
    payment_ref = str(uuid.uuid4())

    return_url = payload.return_url or (
        f"{frontend_url}/pop-up-view?donation=success&provider=paypal&payment_ref={payment_ref}"
        if payload.checkout_view == "popup"
        else f"{frontend_url}/?donation=success&provider=paypal&payment_ref={payment_ref}"
    )
    cancel_url = payload.cancel_url or (
        f"{frontend_url}/pop-up-view?donation=cancelled&provider=paypal"
        if payload.checkout_view == "popup"
        else f"{frontend_url}/?donation=cancelled&provider=paypal"
    )

    params = {
        "cmd": "_xclick",
        "business": payee,
        "amount": f"{charge_amount:.2f}",
        "currency_code": charge_currency_code,
        "item_name": "Donation",
        "item_number": payment_ref,
        "invoice": payment_ref,
        "return": return_url,
        "cancel_return": cancel_url,
        "rm": "2",
        "charset": "utf-8",
        "no_shipping": "1",
        "email": payload.donor.email,
        "first_name": payload.donor.first_name,
        "last_name": payload.donor.last_name,
    }

    return {
        "redirect_url": f"{_paypal_webscr_base()}?{urlencode(params)}",
        "payment_ref": payment_ref,
        "payee_email": payee,
        "charge_currency": charge_currency_code,
        "charge_amount": f"{charge_amount:.2f}",
        "display_amount": format_display_amount(total_display, display_currency),
    }


@router.post("/complete-redirect")
def paypal_complete_redirect(payload: CompletePayPalRedirectRequest) -> CapturePayPalOrderResponse:
    from routers.paypal_connect import resolve_paypal_payee_email_for_checkout

    payee = resolve_paypal_payee_email_for_checkout(payload.campaign_id, payload.checkout_view)
    if not payee:
        raise HTTPException(status_code=400, detail="PayPal is not connected for this page")

    display_currency = payload.currency.lower()
    base_amount, total_display = _resolve_total(payload.amount, display_currency, payload.cover_fees)
    order_id = f"paypal:{payload.paypal_txn_id}" if payload.paypal_txn_id else f"paypal:{payload.payment_ref}"
    saved = _record_paypal_donation(
        order_id=order_id,
        payload=payload,
        base_amount=base_amount,
        total_display=total_display,
    )
    if saved:
        from emails import send_donation_alerts_for_row, send_donation_confirmation_for_row

        send_donation_confirmation_for_row(saved)
        send_donation_alerts_for_row(saved)
    return CapturePayPalOrderResponse(order_id=order_id, status="COMPLETED", recorded=bool(saved))


@router.post("/create-order", response_model=CreatePayPalOrderResponse)
def paypal_create_order(payload: CreatePayPalOrderRequest) -> CreatePayPalOrderResponse:
    from routers.paypal_connect import resolve_paypal_payee_email_for_checkout

    payee = resolve_paypal_payee_email_for_checkout(payload.campaign_id, payload.checkout_view)
    if not payee:
        raise HTTPException(
            status_code=400,
            detail="PayPal is not connected for this page. Connect a PayPal account in admin first.",
        )

    if not paypal_configured():
        raise HTTPException(status_code=503, detail="PayPal API is not configured on the server")

    if payload.frequency != "once":
        raise HTTPException(status_code=400, detail="PayPal is only available for one-time donations")

    display_currency = payload.currency.lower()
    base_amount, total_display = _resolve_total(payload.amount, display_currency, payload.cover_fees)
    frontend_url = resolve_frontend_url()
    return_url = payload.return_url or f"{frontend_url}/pop-up-view?donation=success"
    cancel_url = payload.cancel_url or f"{frontend_url}/pop-up-view?donation=cancelled"

    try:
        created = create_paypal_order(
            total_display=total_display,
            display_currency=display_currency,
            description="Gaza Emergency Donation",
            return_url=return_url,
            cancel_url=cancel_url,
            custom_id=json.dumps(_metadata_payload(payload, base_amount))[:127],
            payee_email=payee,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    charge_currency_code, charge_amount = convert_for_paypal(total_display, display_currency)
    conversion = None
    if charge_currency_code != display_currency.upper():
        conversion = (
            f"PayPal charges in {charge_currency_code}. Your "
            f"{format_display_amount(total_display, display_currency)} donation is approximately "
            f"{format_display_amount(charge_amount, charge_currency_code)}."
        )

    return CreatePayPalOrderResponse(
        order_id=created["order_id"],
        charge_currency=created["charge_currency"],
        charge_amount=float(created["charge_amount"]),
        display_amount=created["display_amount"],
        conversion_note=conversion,
    )


@router.post("/capture-order", response_model=CapturePayPalOrderResponse)
def paypal_capture_order(payload: CapturePayPalOrderRequest) -> CapturePayPalOrderResponse:
    if not paypal_configured():
        raise HTTPException(status_code=503, detail="PayPal is not configured on the server")

    try:
        capture = capture_paypal_order(payload.order_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    status = capture.get("status", "")
    if status != "COMPLETED":
        raise HTTPException(status_code=400, detail="PayPal payment was not completed")

    display_currency = payload.currency.upper()
    base_amount, total_display = _resolve_total(payload.amount, payload.currency.lower(), payload.cover_fees)
    order_id = f"paypal:{payload.order_id}"
    saved = _record_paypal_donation(
        order_id=order_id,
        payload=payload,
        base_amount=base_amount,
        total_display=total_display,
    )
    if saved:
        from emails import send_donation_alerts_for_row, send_donation_confirmation_for_row

        send_donation_confirmation_for_row(saved)
        send_donation_alerts_for_row(saved)

    return CapturePayPalOrderResponse(order_id=payload.order_id, status=status, recorded=bool(saved))


@router.get("/config")
def paypal_config(
    campaign_id: str | None = Query(None),
    checkout_view: Literal["homepage", "popup"] = Query("homepage"),
) -> dict[str, str | bool]:
    from routers.paypal_connect import resolve_paypal_payee_email_for_checkout

    payee = resolve_paypal_payee_email_for_checkout(campaign_id, checkout_view)
    return {
        "configured": bool(payee),
        "merchant_connected": bool(payee),
        "api_configured": paypal_configured(),
        "currency": paypal_checkout_currency(),
        "env": os.getenv("PAYPAL_ENV", "sandbox"),
    }


@router.get("/callback")
def paypal_oauth_callback(
    state: str = Query(...),
    code: str | None = Query(None),
    merchantIdInPayPal: str | None = Query(None),
    merchantId: str | None = Query(None),
) -> RedirectResponse:
    from routers.payment_accounts import handle_root_paypal_callback
    from routers.paypal_connect import handle_org_paypal_callback, handle_paypal_partner_callback

    merchant = merchantIdInPayPal or merchantId
    if merchant:
        redirect = handle_paypal_partner_callback(state, merchant)
        if redirect is not None:
            return redirect
        raise HTTPException(status_code=400, detail="Invalid PayPal connect state")

    if not code:
        raise HTTPException(status_code=400, detail="Missing PayPal authorization code")

    redirect = handle_root_paypal_callback(code, state)
    if redirect is not None:
        return redirect

    redirect = handle_org_paypal_callback(code, state)
    if redirect is not None:
        return redirect

    raise HTTPException(status_code=400, detail="Invalid PayPal connect state")
