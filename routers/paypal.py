from __future__ import annotations

import json
import os
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from currency import (
    calculate_total_with_fees,
    convert_for_paypal,
    estimate_processing_fee,
    format_display_amount,
    paypal_checkout_currency,
)
from paypal_client import capture_paypal_order, create_paypal_order, paypal_configured
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


class CreatePayPalOrderRequest(BaseModel):
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
    return_url: str | None = None
    cancel_url: str | None = None


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


@router.post("/create-order", response_model=CreatePayPalOrderResponse)
def paypal_create_order(payload: CreatePayPalOrderRequest) -> CreatePayPalOrderResponse:
    if not paypal_configured():
        raise HTTPException(status_code=503, detail="PayPal is not configured on the server")

    if payload.frequency != "once":
        raise HTTPException(status_code=400, detail="PayPal is only available for one-time donations")

    display_currency = payload.currency.lower()
    base_amount, total_display = _resolve_total(payload.amount, display_currency, payload.cover_fees)
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
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
    cover_fees = payload.cover_fees
    if cover_fees:
        processing_fee = max(0.0, round(total_display - base_amount, 2))
        payout_amount = base_amount
    else:
        processing_fee = estimate_processing_fee(base_amount, display_currency)
        payout_amount = max(0.0, round(base_amount - processing_fee, 2))

    row = {
        "stripe_payment_intent_id": f"paypal:{payload.order_id}",
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
        "campaign_id": payload.campaign_id,
        "status": "succeeded",
        "fee_covered": cover_fees,
        "platform_fee": 0,
        "processing_fee": processing_fee,
        "payout_amount": payout_amount,
    }
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

    recorded = False
    if supabase_enabled():
        recorded = insert_donation(row) is not None

    return CapturePayPalOrderResponse(order_id=payload.order_id, status=status, recorded=recorded)


@router.get("/config")
def paypal_config() -> dict[str, str | bool]:
    return {
        "configured": paypal_configured(),
        "currency": paypal_checkout_currency(),
        "env": os.getenv("PAYPAL_ENV", "sandbox"),
    }
