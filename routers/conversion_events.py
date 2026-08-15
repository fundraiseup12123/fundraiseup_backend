from __future__ import annotations

import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from db import rest_insert_result

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["conversion-analytics"])

_ALLOWED_EVENTS = {
    "amount_view",
    "amount_selected",
    "monthly_upsell_view",
    "monthly_upsell_accept",
    "monthly_upsell_decline",
    "monthly_upsell_back",
    "details_view",
    "details_submitted",
    "payment_view",
    "payment_method_selected",
    "checkout_started",
    "payment_session_created",
    "donation_success",
    "donation_error",
    "meta_iab_detected",
    "meta_iab_redirect_attempt",
    "meta_iab_redirect_result",
}


class ConversionUtm(BaseModel):
    source: str | None = Field(default=None, max_length=500)
    medium: str | None = Field(default=None, max_length=500)
    campaign: str | None = Field(default=None, max_length=500)
    term: str | None = Field(default=None, max_length=500)
    content: str | None = Field(default=None, max_length=500)


class ConversionDevice(BaseModel):
    os: str | None = Field(default=None, max_length=80)
    browser: str | None = Field(default=None, max_length=80)
    type: str | None = Field(default=None, max_length=80)
    country: str | None = Field(default=None, max_length=8)


class ConversionEventRequest(BaseModel):
    event_id: str = Field(
        min_length=8,
        max_length=100,
        pattern=r"^event_[A-Za-z0-9-]+$",
    )
    session_id: str = Field(
        min_length=8,
        max_length=100,
        pattern=r"^session_[A-Za-z0-9-]+$",
    )
    event_name: str = Field(min_length=3, max_length=80)
    campaign_id: str | None = Field(default=None, max_length=100)
    checkout_view: Literal["homepage", "popup", "landing"] = "homepage"
    funnel_step: str | None = Field(default=None, max_length=80)
    payment_method: str | None = Field(default=None, max_length=50)
    payment_processor: str | None = Field(default=None, max_length=50)
    frequency: Literal["once", "monthly"] | None = None
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    cover_fees: bool | None = None
    transaction_id: str | None = Field(default=None, max_length=150)
    utm: ConversionUtm | None = None
    device: ConversionDevice | None = None
    metadata: dict[str, Any] | None = None


def _compact_model(value: BaseModel | None) -> dict[str, Any] | None:
    if value is None:
        return None
    data = value.model_dump(exclude_none=True)
    return data or None


def _uuid_or_none(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError):
        return None


def _safe_metadata(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    safe: dict[str, Any] = {}
    for key, value in list(raw.items())[:20]:
        clean_key = str(key).strip()[:60]
        if not clean_key or not isinstance(value, (str, int, float, bool)):
            continue
        safe[clean_key] = value[:150] if isinstance(value, str) else value
    return safe or None


@router.post("/conversion-event", status_code=202)
def record_conversion_event(payload: ConversionEventRequest) -> dict[str, bool]:
    """Best-effort first-party funnel telemetry. Never blocks donation checkout."""
    event_name = payload.event_name.strip().lower()
    if event_name not in _ALLOWED_EVENTS:
        return {"accepted": False}

    row: dict[str, Any] = {
        "event_id": payload.event_id.strip(),
        "session_id": payload.session_id.strip(),
        "event_name": event_name,
        "campaign_id": _uuid_or_none(payload.campaign_id),
        "checkout_view": payload.checkout_view,
        "funnel_step": payload.funnel_step,
        "payment_method": payload.payment_method,
        "payment_processor": payload.payment_processor,
        "frequency": payload.frequency,
        "amount": payload.amount,
        "currency": payload.currency.upper() if payload.currency else None,
        "cover_fees": payload.cover_fees,
        "transaction_id": payload.transaction_id,
        "utm": _compact_model(payload.utm),
        "device": _compact_model(payload.device),
        "metadata": _safe_metadata(payload.metadata),
    }
    row = {key: value for key, value in row.items() if value is not None}

    _saved, error = rest_insert_result(
        "checkout_attempts",
        row,
        on_conflict="event_id",
    )
    if error:
        # Deploying code before the migration must not impact payments.
        logger.info("Conversion event not persisted (%s): %s", event_name, error)
        return {"accepted": False}
    return {"accepted": True}
