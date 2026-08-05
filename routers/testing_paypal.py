"""
Isolated PayPal sandbox API for /testing-paypal.

Credentials: backend/.env.testing-paypal ONLY (gitignored).
Never reads production .env PAYPAL_* keys.

Frontend calls via /api/backend/testing-paypal/...
Client id for the JS SDK is returned by GET /testing-paypal/status at runtime.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from currency import calculate_total_with_fees
from frontend_url import resolve_frontend_url
from testing_paypal_client import (
    assert_testing_live_safe,
    capture_testing_order,
    create_testing_client_token,
    create_testing_order,
    create_testing_subscription,
    ensure_testing_plan,
    get_testing_subscription,
    testing_paypal_configured,
    testing_paypal_status,
)

router = APIRouter(prefix="/testing-paypal", tags=["testing-paypal"])

PaymentMethodLabel = Literal["card", "google_pay", "apple_pay", "paypal"]
Frequency = Literal["once", "monthly"]


class TestingDonor(BaseModel):
    first_name: str = Field(default="Test", min_length=1, max_length=80)
    last_name: str = Field(default="Donor", min_length=1, max_length=80)
    email: str = Field(default="test@example.com", min_length=3, max_length=254)


class TestingPaypalBaseRequest(BaseModel):
    amount: float = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    frequency: Frequency = "once"
    cover_fees: bool = False
    payment_method: PaymentMethodLabel = "paypal"
    donor: TestingDonor | None = None
    return_url: str | None = None
    cancel_url: str | None = None


class CreateOrderRequest(TestingPaypalBaseRequest):
    frequency: Literal["once"] = "once"


class CaptureOrderRequest(BaseModel):
    order_id: str = Field(min_length=5, max_length=64)
    payment_method: PaymentMethodLabel = "paypal"


class EnsurePlanRequest(TestingPaypalBaseRequest):
    frequency: Literal["monthly"] = "monthly"


class CreateSubscriptionRequest(TestingPaypalBaseRequest):
    frequency: Literal["monthly"] = "monthly"
    plan_id: str | None = None


class ActivateSubscriptionRequest(BaseModel):
    subscription_id: str = Field(min_length=5, max_length=64)
    payment_method: PaymentMethodLabel = "paypal"


def _charge_total(amount: float, currency: str, cover_fees: bool) -> float:
    if cover_fees:
        return calculate_total_with_fees(amount, currency)
    return round(float(amount), 2)


def _default_urls(request: TestingPaypalBaseRequest) -> tuple[str, str]:
    base = resolve_frontend_url().rstrip("/")
    return_url = (request.return_url or f"{base}/testing-paypal/return").strip()
    cancel_url = (request.cancel_url or f"{base}/testing-paypal/cancel").strip()
    return return_url, cancel_url


def _require_configured() -> None:
    if not testing_paypal_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Testing PayPal is not configured. Add credentials to "
                "backend/.env.testing-paypal (see .env.testing-paypal.example). "
                "Production .env PAYPAL_* vars are not used by this route."
            ),
        )


@router.get("/status")
def status() -> dict[str, Any]:
    return testing_paypal_status()


@router.get("/client-token")
def client_token() -> dict[str, Any]:
    """Optional token so the JS SDK can expose CardFields when the app supports ACDC."""
    _require_configured()
    try:
        token = create_testing_client_token()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"client_token": token}


@router.post("/create-order")
def create_order(body: CreateOrderRequest) -> dict[str, Any]:
    _require_configured()
    if body.frequency != "once":
        raise HTTPException(status_code=400, detail="create-order is for one-time donations only")

    total = _charge_total(body.amount, body.currency, body.cover_fees)
    return_url, cancel_url = _default_urls(body)
    donor = body.donor or TestingDonor()

    try:
        assert_testing_live_safe(total)
        result = create_testing_order(
            total_display=total,
            display_currency=body.currency.upper(),
            description=f"Testing one-time donation ({body.payment_method})",
            return_url=return_url,
            cancel_url=cancel_url,
            custom_id=f"testing-{body.payment_method}-{donor.email[:40]}",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        **result,
        "frequency": "once",
        "payment_method": body.payment_method,
        "base_amount": body.amount,
        "total_amount": total,
        "currency": body.currency.upper(),
    }


@router.post("/capture-order")
def capture_order(body: CaptureOrderRequest) -> dict[str, Any]:
    _require_configured()
    try:
        result = capture_testing_order(body.order_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    status_value = str(result.get("status") or "").upper()
    if status_value not in {"COMPLETED", "CAPTURED"}:
        # PayPal returns COMPLETED on successful capture; reject other states.
        if status_value not in {"PENDING", "APPROVED"}:
            raise HTTPException(
                status_code=400,
                detail=f"Capture not successful (status={status_value or 'unknown'})",
            )

    return {
        "order_id": result["order_id"],
        "status": result["status"],
        "capture_id": result.get("capture_id"),
        "transaction_id": result.get("transaction_id"),
        "payment_method": body.payment_method,
        "verified": status_value in {"COMPLETED", "CAPTURED"},
    }


@router.post("/ensure-plan")
def ensure_plan(body: EnsurePlanRequest) -> dict[str, Any]:
    _require_configured()
    total = _charge_total(body.amount, body.currency, body.cover_fees)
    try:
        assert_testing_live_safe(total)
        result = ensure_testing_plan(
            total_display=total,
            display_currency=body.currency.upper(),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        **result,
        "frequency": "monthly",
        "payment_method": body.payment_method,
        "base_amount": body.amount,
        "total_amount": total,
        "currency": body.currency.upper(),
    }


@router.post("/create-subscription")
def create_subscription(body: CreateSubscriptionRequest) -> dict[str, Any]:
    _require_configured()
    total = _charge_total(body.amount, body.currency, body.cover_fees)
    return_url, cancel_url = _default_urls(body)
    donor = body.donor or TestingDonor()

    try:
        assert_testing_live_safe(total)
        plan_id = body.plan_id
        plan_meta: dict[str, Any] = {}
        if not plan_id:
            plan_meta = ensure_testing_plan(
                total_display=total,
                display_currency=body.currency.upper(),
            )
            plan_id = str(plan_meta["plan_id"])

        result = create_testing_subscription(
            plan_id=plan_id,
            return_url=return_url,
            cancel_url=cancel_url,
            custom_id=f"testing-sub-{body.payment_method}",
            subscriber={
                "name": {
                    "given_name": donor.first_name,
                    "surname": donor.last_name,
                },
                "email_address": donor.email,
            },
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        **result,
        "product_id": plan_meta.get("product_id"),
        "charge_currency": plan_meta.get("charge_currency"),
        "charge_amount": plan_meta.get("charge_amount"),
        "display_amount": plan_meta.get("display_amount"),
        "frequency": "monthly",
        "payment_method": body.payment_method,
        "base_amount": body.amount,
        "total_amount": total,
        "currency": body.currency.upper(),
    }


@router.get("/subscription/{subscription_id}")
def subscription_status(subscription_id: str) -> dict[str, Any]:
    _require_configured()
    try:
        return get_testing_subscription(subscription_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/activate-subscription")
def activate_subscription(body: ActivateSubscriptionRequest) -> dict[str, Any]:
    """Verify subscription after buyer approval (PayPal activates on approve)."""
    _require_configured()
    try:
        result = get_testing_subscription(body.subscription_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    status_value = str(result.get("status") or "").upper()
    success_states = {"ACTIVE", "APPROVED"}
    pending_states = {"APPROVAL_PENDING", "SUSPENDED"}

    if status_value not in success_states and status_value not in pending_states:
        raise HTTPException(
            status_code=400,
            detail=f"Subscription not active (status={status_value or 'unknown'})",
        )

    return {
        **result,
        "payment_method": body.payment_method,
        "verified": status_value in success_states,
        "pending_approval": status_value in pending_states,
    }
