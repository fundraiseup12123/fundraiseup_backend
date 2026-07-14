from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

import httpx


def nowpayments_api_base() -> str:
    explicit = (os.getenv("NOWPAYMENTS_API_BASE") or "").strip().rstrip("/")
    if explicit:
        return explicit
    env = (os.getenv("NOWPAYMENTS_ENV") or "live").strip().lower()
    if env in {"sandbox", "test"}:
        return "https://api-sandbox.nowpayments.io/v1"
    return "https://api.nowpayments.io/v1"


def api_key_hint(api_key: str) -> str:
    key = (api_key or "").strip()
    if len(key) <= 8:
        return "••••" + key[-2:] if key else "••••"
    return f"{key[:4]}…{key[-4:]}"


def verify_api_key(api_key: str) -> bool:
    """Validate credentials against NOWPayments status endpoint."""
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.get(
                f"{nowpayments_api_base()}/status",
                headers={"x-api-key": api_key.strip()},
            )
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def create_invoice(
    *,
    api_key: str,
    price_amount: float,
    price_currency: str,
    order_id: str,
    order_description: str,
    ipn_callback_url: str,
    success_url: str,
    cancel_url: str,
) -> dict[str, Any]:
    payload = {
        "price_amount": round(float(price_amount), 2),
        "price_currency": price_currency.lower(),
        "order_id": order_id,
        "order_description": order_description[:400],
        "ipn_callback_url": ipn_callback_url,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "is_fixed_rate": False,
        # Donor covers NOWPayments/network fees so received amount matches invoice
        # and payments are less likely to stick on partially_paid.
        "is_fee_paid_by_user": True,
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            f"{nowpayments_api_base()}/invoice",
            headers={
                "x-api-key": api_key.strip(),
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if response.status_code >= 400:
        detail = response.text
        try:
            data = response.json()
            detail = data.get("message") or data.get("detail") or detail
        except Exception:
            pass
        raise RuntimeError(detail or f"NOWPayments invoice failed ({response.status_code})")
    data = response.json()
    if not isinstance(data, dict) or not data.get("invoice_url"):
        raise RuntimeError("NOWPayments did not return an invoice URL")
    return data


def get_payment_status(*, api_key: str, payment_id: str) -> dict[str, Any] | None:
    with httpx.Client(timeout=20.0) as client:
        response = client.get(
            f"{nowpayments_api_base()}/payment/{payment_id}",
            headers={"x-api-key": api_key.strip()},
        )
    if response.status_code >= 400:
        return None
    data = response.json()
    return data if isinstance(data, dict) else None


def verify_ipn_signature(payload: dict[str, Any], signature: str | None, ipn_secret: str) -> bool:
    if not signature or not ipn_secret:
        return False
    sorted_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hmac.new(
        ipn_secret.encode("utf-8"),
        sorted_json.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(digest, signature)
