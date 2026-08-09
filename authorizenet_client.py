"""Authorize.net JSON API helpers (Accept.js / Apple Pay / PayPal Express / ARB)."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_http = httpx.Client(timeout=30.0)


def _clean_env(name: str, fallback: str = "") -> str:
    raw = os.getenv(name, fallback) or fallback
    return raw.strip().strip('"').strip("'")


def authorizenet_default_env() -> str:
    value = _clean_env("AUTHORIZE_NET_ENV", "production").lower()
    if value in {"sandbox", "production", "prod", "live"}:
        if value in {"prod", "live"}:
            return "production"
        return value
    return "production"


def api_endpoint(env: str | None = None) -> str:
    mode = (env or authorizenet_default_env()).strip().lower()
    if mode == "sandbox":
        return "https://apitest.authorize.net/xml/v1/request.api"
    return "https://api.authorize.net/xml/v1/request.api"


def acceptjs_script_url(env: str | None = None) -> str:
    mode = (env or authorizenet_default_env()).strip().lower()
    if mode == "sandbox":
        return "https://jstest.authorize.net/v1/Accept.js"
    return "https://js.authorize.net/v1/Accept.js"


def credential_hint(value: str, keep: int = 4) -> str:
    text = (value or "").strip()
    if len(text) <= keep * 2:
        return text[:2] + "…" if text else ""
    return f"{text[:keep]}…{text[-keep:]}"


def _messages_ok(payload: dict[str, Any]) -> bool:
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, dict):
        return False
    return str(messages.get("resultCode") or "").lower() == "ok"


def _first_error(payload: dict[str, Any]) -> str:
    txn = payload.get("transactionResponse") if isinstance(payload, dict) else None
    if isinstance(txn, dict):
        response_code = str(txn.get("responseCode") or "")
        errors = txn.get("errors")
        if isinstance(errors, list) and errors:
            text = str(errors[0].get("errorText") or errors[0].get("errorCode") or "").strip()
            if text:
                return text
        if isinstance(errors, dict):
            text = str(errors.get("errorText") or errors.get("errorCode") or "").strip()
            if text:
                return text
        if response_code == "2":
            return "Card was declined"
        if response_code == "3":
            return "Transaction error"
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if isinstance(messages, dict):
        items = messages.get("message")
        if isinstance(items, list) and items:
            text = str(items[0].get("text") or items[0].get("code") or "").strip()
            if text:
                return text
        if isinstance(items, dict):
            text = str(items.get("text") or items.get("code") or "").strip()
            if text:
                return text
    return "Authorize.net request failed"


def _post(request_body: dict[str, Any], *, env: str) -> dict[str, Any]:
    url = api_endpoint(env)
    try:
        response = _http.post(url, json=request_body, headers={"Content-Type": "application/json"})
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning("Authorize.net API error: %s", exc)
        raise RuntimeError(f"Authorize.net API unreachable: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Authorize.net returned an invalid response")
    return data


def authenticate_test(api_login_id: str, transaction_key: str, *, env: str = "production") -> bool:
    body = {
        "authenticateTestRequest": {
            "merchantAuthentication": {
                "name": api_login_id,
                "transactionKey": transaction_key,
            }
        }
    }
    try:
        data = _post(body, env=env)
    except RuntimeError:
        return False
    return _messages_ok(data)


def get_merchant_details(
    api_login_id: str,
    transaction_key: str,
    *,
    env: str = "production",
) -> dict[str, Any]:
    body = {
        "getMerchantDetailsRequest": {
            "merchantAuthentication": {
                "name": api_login_id,
                "transactionKey": transaction_key,
            }
        }
    }
    data = _post(body, env=env)
    if not _messages_ok(data):
        raise RuntimeError(_first_error(data))
    return data


def create_transaction_opaque(
    *,
    api_login_id: str,
    transaction_key: str,
    amount: float,
    currency: str,
    data_descriptor: str,
    data_value: str,
    order_invoice: str | None = None,
    customer_email: str | None = None,
    env: str = "production",
) -> dict[str, Any]:
    transaction_request: dict[str, Any] = {
        "transactionType": "authCaptureTransaction",
        "amount": f"{amount:.2f}",
        "currencyCode": (currency or "USD").upper(),
        "payment": {
            "opaqueData": {
                "dataDescriptor": data_descriptor,
                "dataValue": data_value,
            }
        },
    }
    if order_invoice:
        transaction_request["order"] = {"invoiceNumber": order_invoice[:20]}
    if customer_email:
        transaction_request["customer"] = {"email": customer_email[:255]}

    body = {
        "createTransactionRequest": {
            "merchantAuthentication": {
                "name": api_login_id,
                "transactionKey": transaction_key,
            },
            "transactionRequest": transaction_request,
        }
    }
    data = _post(body, env=env)
    txn = data.get("transactionResponse") if isinstance(data.get("transactionResponse"), dict) else {}
    # Approved (1) only — held-for-review (4) is not a completed gift.
    response_code = str(txn.get("responseCode") or "")
    if not _messages_ok(data) or response_code != "1":
        raise RuntimeError(_first_error(data))
    return {
        "transaction_id": str(txn.get("transId") or ""),
        "auth_code": str(txn.get("authCode") or ""),
        "response_code": response_code,
        "account_type": str(txn.get("accountType") or ""),
        "raw": data,
    }


def create_paypal_express(
    *,
    api_login_id: str,
    transaction_key: str,
    amount: float,
    currency: str,
    success_url: str,
    cancel_url: str,
    order_invoice: str | None = None,
    customer_email: str | None = None,
    env: str = "production",
) -> dict[str, Any]:
    transaction_request: dict[str, Any] = {
        "transactionType": "authCaptureTransaction",
        "amount": f"{amount:.2f}",
        "currencyCode": (currency or "USD").upper(),
        "payment": {
            "payPal": {
                "successUrl": success_url,
                "cancelUrl": cancel_url,
            }
        },
    }
    if order_invoice:
        transaction_request["order"] = {"invoiceNumber": order_invoice[:20]}
    if customer_email:
        transaction_request["customer"] = {"email": customer_email[:255]}

    body = {
        "createTransactionRequest": {
            "merchantAuthentication": {
                "name": api_login_id,
                "transactionKey": transaction_key,
            },
            "transactionRequest": transaction_request,
        }
    }
    data = _post(body, env=env)
    txn = data.get("transactionResponse") if isinstance(data.get("transactionResponse"), dict) else {}
    secure = txn.get("secureAcceptance") if isinstance(txn.get("secureAcceptance"), dict) else {}
    redirect_url = str(
        secure.get("SecureAcceptanceUrl")
        or secure.get("secureAcceptanceUrl")
        or txn.get("refTransId")
        or ""
    )
    # Some Anet PayPal responses put the redirect under messages / extra fields
    if not redirect_url:
        for key in ("SecureAcceptanceUrl", "secureAcceptanceUrl", "redirectUrl"):
            if data.get(key):
                redirect_url = str(data[key])
                break
    if not _messages_ok(data) and not redirect_url:
        raise RuntimeError(_first_error(data))
    return {
        "transaction_id": str(txn.get("transId") or ""),
        "redirect_url": redirect_url,
        "raw": data,
    }


def complete_paypal_express(
    *,
    api_login_id: str,
    transaction_key: str,
    amount: float,
    currency: str,
    payer_id: str,
    ref_trans_id: str | None = None,
    env: str = "production",
) -> dict[str, Any]:
    paypal_payload: dict[str, Any] = {"payerID": payer_id}
    transaction_request: dict[str, Any] = {
        "transactionType": "authCaptureTransaction",
        "amount": f"{amount:.2f}",
        "currencyCode": (currency or "USD").upper(),
        "payment": {"payPal": paypal_payload},
    }
    if ref_trans_id:
        transaction_request["refTransId"] = ref_trans_id

    body = {
        "createTransactionRequest": {
            "merchantAuthentication": {
                "name": api_login_id,
                "transactionKey": transaction_key,
            },
            "transactionRequest": transaction_request,
        }
    }
    data = _post(body, env=env)
    txn = data.get("transactionResponse") if isinstance(data.get("transactionResponse"), dict) else {}
    response_code = str(txn.get("responseCode") or "")
    if not _messages_ok(data) or response_code != "1":
        raise RuntimeError(_first_error(data))
    return {
        "transaction_id": str(txn.get("transId") or ""),
        "auth_code": str(txn.get("authCode") or ""),
        "response_code": response_code,
        "raw": data,
    }


def create_arb_subscription_from_opaque(
    *,
    api_login_id: str,
    transaction_key: str,
    amount: float,
    currency: str,
    data_descriptor: str,
    data_value: str,
    customer_email: str,
    first_name: str,
    last_name: str,
    subscription_name: str,
    env: str = "production",
) -> dict[str, Any]:
    """Create a monthly ARB subscription using Accept.js opaque payment data."""
    body = {
        "ARBCreateSubscriptionRequest": {
            "merchantAuthentication": {
                "name": api_login_id,
                "transactionKey": transaction_key,
            },
            "subscription": {
                "name": (subscription_name or "Monthly donation")[:50],
                "paymentSchedule": {
                    "interval": {"length": 1, "unit": "months"},
                    "startDate": __import__("datetime").date.today().isoformat(),
                    "totalOccurrences": 9999,
                },
                "amount": f"{amount:.2f}",
                "payment": {
                    "opaqueData": {
                        "dataDescriptor": data_descriptor,
                        "dataValue": data_value,
                    }
                },
                "order": {
                    "invoiceNumber": f"sub{__import__('time').time_ns() % 10**12}"[:20],
                    "description": "Monthly donation",
                },
                "customer": {"email": customer_email[:255]},
                "billTo": {
                    "firstName": (first_name or "Donor")[:50],
                    "lastName": (last_name or "Donor")[:50],
                },
            },
        }
    }
    # currency is not always accepted on ARB; merchant account currency applies
    _ = currency
    data = _post(body, env=env)
    if not _messages_ok(data):
        raise RuntimeError(_first_error(data))
    sub_id = str(data.get("subscriptionId") or "")
    if not sub_id:
        raise RuntimeError("Authorize.net did not return a subscription id")
    return {"subscription_id": sub_id, "raw": data}
