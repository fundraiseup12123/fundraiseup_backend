from __future__ import annotations

import os
from typing import Any

import httpx

from currency import convert_for_paypal, format_display_amount

PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID") or os.getenv("NEXT_PUBLIC_PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "")
PAYPAL_ENV = os.getenv("PAYPAL_ENV", "sandbox").lower()


def paypal_configured() -> bool:
    return bool(PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET)


def paypal_api_base() -> str:
    if PAYPAL_ENV == "live":
        return "https://api-m.paypal.com"
    return "https://api-m.sandbox.paypal.com"


def _paypal_access_token() -> str:
    if not paypal_configured():
        raise RuntimeError("PayPal is not configured")

    response = httpx.post(
        f"{paypal_api_base()}/v1/oauth2/token",
        data={"grant_type": "client_credentials"},
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        headers={"Accept": "application/json"},
        timeout=30.0,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("PayPal auth failed")
    return token


def create_paypal_order(
    *,
    total_display: float,
    display_currency: str,
    description: str,
    return_url: str,
    cancel_url: str,
    custom_id: str | None = None,
) -> dict[str, Any]:
    charge_currency, charge_amount = convert_for_paypal(total_display, display_currency)
    amount_value = f"{charge_amount:.2f}"

    purchase_unit: dict[str, Any] = {
        "amount": {
            "currency_code": charge_currency,
            "value": amount_value,
        },
        "description": description[:127],
    }
    if custom_id:
        purchase_unit["custom_id"] = custom_id[:127]

    payload = {
        "intent": "CAPTURE",
        "purchase_units": [purchase_unit],
        "application_context": {
            "brand_name": "Gaza Emergency Appeal",
            "locale": "en-US",
            "landing_page": "NO_PREFERENCE",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "PAY_NOW",
            "return_url": return_url,
            "cancel_url": cancel_url,
        },
    }

    token = _paypal_access_token()
    response = httpx.post(
        f"{paypal_api_base()}/v2/checkout/orders",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        detail = response.text
        try:
            detail = response.json().get("message", detail)
        except Exception:
            pass
        raise RuntimeError(detail or "Unable to create PayPal order")

    body = response.json()
    order_id = body.get("id")
    if not order_id:
        raise RuntimeError("PayPal did not return an order id")
    return {
        "order_id": order_id,
        "charge_currency": charge_currency,
        "charge_amount": charge_amount,
        "display_amount": format_display_amount(total_display, display_currency),
    }


def capture_paypal_order(order_id: str) -> dict[str, Any]:
    token = _paypal_access_token()
    response = httpx.post(
        f"{paypal_api_base()}/v2/checkout/orders/{order_id}/capture",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        detail = response.text
        try:
            detail = response.json().get("message", detail)
        except Exception:
            pass
        raise RuntimeError(detail or "Unable to capture PayPal payment")

    return response.json()
