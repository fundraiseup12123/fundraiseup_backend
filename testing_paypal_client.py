"""
Isolated PayPal helpers for /testing-paypal only (sandbox or live).

Credentials are loaded ONLY from backend/.env.testing-paypal (gitignored),
or from TESTING_PAYPAL_* process env (Railway). They are never written into
process env from the file, and never read from production PAYPAL_* / Stripe.

OAuth + API base are private to this module so campaign PayPal
(paypal_client / routers/paypal) stays untouched.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from currency import format_display_amount, convert_to_reporting
from paypal_client import approve_link_from_order

logger = logging.getLogger(__name__)

_TESTING_ENV_FILE = Path(__file__).resolve().parent / ".env.testing-paypal"
_http = httpx.Client(timeout=30.0)
_plan_lock = threading.Lock()
# (currency_upper, amount_value, plan_variant) -> {"product_id", "plan_id"}
_plan_cache: dict[tuple[str, str, str], dict[str, str]] = {}
_product_id: str | None = None
_product_lock = threading.Lock()
_file_lock = threading.Lock()
_file_cache: dict[str, str] = {}
_file_mtime: float | None = None
_token_lock = threading.Lock()
# cache_key -> (access_token, expires_at)
_token_cache: dict[str, tuple[str, float]] = {}


def _testing_api_base(env: str | None = None) -> str:
    mode = (env or testing_paypal_env()).lower()
    if mode == "live":
        return "https://api-m.paypal.com"
    return "https://api-m.sandbox.paypal.com"


def _testing_access_token(
    client_id: str,
    client_secret: str,
    env: str,
) -> str:
    """OAuth for testing credentials only — separate cache from production PayPal."""
    cid = (client_id or "").strip()
    secret = (client_secret or "").strip()
    if not cid or not secret:
        raise RuntimeError("Testing PayPal is not configured")

    api_base = _testing_api_base(env)
    cache_key = f"{api_base}|{cid}"
    now = time.time()
    with _token_lock:
        cached = _token_cache.get(cache_key)
        if cached and cached[1] > now + 60:
            return cached[0]

    try:
        response = _http.post(
            f"{api_base}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(cid, secret),
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        try:
            detail = exc.response.json().get("error_description", detail)
        except Exception:
            pass
        raise RuntimeError(detail or "Testing PayPal credentials rejected") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("Unable to reach PayPal (testing)") from exc

    body = response.json()
    token = body.get("access_token")
    if not token:
        raise RuntimeError("PayPal did not return an access token")
    expires_in = int(body.get("expires_in") or 28800)
    expires_at = now + max(120, expires_in - 120)
    with _token_lock:
        _token_cache[cache_key] = (str(token), expires_at)
    return str(token)


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Unable to read %s", path)
        return values

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _testing_file_values() -> dict[str, str]:
    """Load .env.testing-paypal into a private cache (not os.environ)."""
    global _file_cache, _file_mtime
    with _file_lock:
        try:
            mtime = _TESTING_ENV_FILE.stat().st_mtime if _TESTING_ENV_FILE.is_file() else None
        except OSError:
            mtime = None
        if mtime == _file_mtime and _file_cache is not None:
            return _file_cache
        _file_cache = _parse_env_file(_TESTING_ENV_FILE)
        _file_mtime = mtime
        return _file_cache


def _clean_env(name: str, fallback: str = "") -> str:
    # Prefer dedicated testing file, then process env TESTING_* only — never PAYPAL_*.
    file_vals = _testing_file_values()
    if name in file_vals and file_vals[name]:
        return file_vals[name]
    raw = os.getenv(name, fallback) or fallback
    return raw.strip().strip('"').strip("'")


def testing_paypal_client_id() -> str:
    return _clean_env("TESTING_PAYPAL_CLIENT_ID")


def testing_paypal_client_secret() -> str:
    return _clean_env("TESTING_PAYPAL_CLIENT_SECRET")


def testing_paypal_env() -> str:
    explicit = _clean_env("TESTING_PAYPAL_ENV").lower()
    if explicit in {"sandbox", "live"}:
        return explicit
    return "sandbox"


def testing_paypal_allow_live() -> bool:
    return _clean_env("TESTING_PAYPAL_ALLOW_LIVE").lower() in {"1", "true", "yes"}


def testing_paypal_live_max_amount() -> float:
    raw = _clean_env("TESTING_PAYPAL_LIVE_MAX_AMOUNT", "5")
    try:
        return float(raw)
    except ValueError:
        return 5.0


def testing_paypal_configured() -> bool:
    if not (testing_paypal_client_id() and testing_paypal_client_secret()):
        return False
    if testing_paypal_env() == "live" and not testing_paypal_allow_live():
        return False
    return True


def assert_testing_live_safe(amount: float) -> None:
    """Block oversized live charges on the isolated testing surface."""
    if testing_paypal_env() != "live":
        return
    if not testing_paypal_allow_live():
        raise RuntimeError(
            "TESTING_PAYPAL_ENV=live requires TESTING_PAYPAL_ALLOW_LIVE=1 in "
            ".env.testing-paypal (production .env is not used)."
        )
    cap = testing_paypal_live_max_amount()
    if amount > cap:
        raise RuntimeError(
            f"Live testing amount {amount} exceeds TESTING_PAYPAL_LIVE_MAX_AMOUNT={cap}. "
            "Lower the gift amount or raise the cap in .env.testing-paypal only."
        )


def testing_paypal_creds() -> dict[str, str]:
    """Explicit credentials for testing PayPal calls only."""
    return {
        "client_id": testing_paypal_client_id(),
        "client_secret": testing_paypal_client_secret(),
        "env": testing_paypal_env(),
    }


def convert_for_testing_paypal(total_amount: float, display_currency: str) -> tuple[str, float]:
    """Charge currency for testing flow — prefers TESTING_PAYPAL_CURRENCY from the testing file."""
    target = (_clean_env("TESTING_PAYPAL_CURRENCY") or "USD").upper()
    if display_currency.upper() == target:
        return target, round(total_amount, 2)
    converted = convert_to_reporting(total_amount, display_currency, target)
    return target, round(converted, 2)


def testing_paypal_status() -> dict[str, Any]:
    configured = testing_paypal_configured()
    cid = testing_paypal_client_id()
    from_file = bool(_testing_file_values().get("TESTING_PAYPAL_CLIENT_ID"))
    env = testing_paypal_env()
    return {
        "configured": configured,
        "env": env,
        "client_id": cid if configured else "",
        "has_secret": bool(testing_paypal_client_secret()),
        "mode": "isolated-testing",
        "credential_source": (
            ".env.testing-paypal" if from_file else "TESTING_PAYPAL_* process env"
        ),
        "env_file_present": _TESTING_ENV_FILE.is_file(),
        "live_allowed": testing_paypal_allow_live() if env == "live" else False,
        "live_max_amount": testing_paypal_live_max_amount() if env == "live" else None,
        "warning": (
            "These testing credentials are LIVE PayPal keys. Real charges possible. "
            "Prefer a sandbox app when available."
            if env == "live"
            else None
        ),
    }


def _auth_headers() -> dict[str, str]:
    creds = testing_paypal_creds()
    if not creds["client_id"] or not creds["client_secret"]:
        raise RuntimeError(
            "Testing PayPal is not configured. Set TESTING_PAYPAL_CLIENT_ID and "
            "TESTING_PAYPAL_CLIENT_SECRET."
        )
    token = _testing_access_token(
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        env=creds["env"],
    )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


_client_token_lock = threading.Lock()
# (api_base|client_id) -> (client_token, expires_at_monotonic)
_client_token_cache: dict[str, tuple[str, float]] = {}
# PayPal client tokens are typically valid ~1h; refresh early to avoid expiry mid-checkout.
_CLIENT_TOKEN_TTL_SEC = 45 * 60


def create_testing_client_token() -> str:
    """Client token for PayPal JS Card Fields (Advanced Checkout), when the app supports it."""
    if not testing_paypal_configured():
        raise RuntimeError("Testing PayPal is not configured")
    creds = testing_paypal_creds()
    api_base = _testing_api_base(creds["env"])
    cache_key = f"{api_base}|{creds['client_id']}"
    now = time.monotonic()

    with _client_token_lock:
        cached = _client_token_cache.get(cache_key)
        if cached and cached[1] > now:
            return cached[0]

    headers = _auth_headers()
    response = _http.post(
        f"{api_base}/v1/identity/generate-token",
        headers={**headers, "Accept-Language": "en_US"},
        json={},
    )
    if response.status_code >= 400:
        raise RuntimeError(_paypal_error_detail(response, "Unable to generate PayPal client token"))
    body = response.json()
    token = body.get("client_token")
    if not token:
        raise RuntimeError("PayPal did not return a client_token")
    token_str = str(token)

    with _client_token_lock:
        _client_token_cache[cache_key] = (token_str, now + _CLIENT_TOKEN_TTL_SEC)
    return token_str


def _paypal_error_detail(response: httpx.Response, fallback: str) -> str:
    detail = response.text
    try:
        body = response.json()
        detail = (
            body.get("message")
            or body.get("error_description")
            or body.get("details", detail)
            or detail
        )
        if isinstance(detail, list):
            detail = "; ".join(
                str(item.get("description") or item.get("issue") or item) for item in detail
            )
    except Exception:
        pass
    return str(detail or fallback)


def create_testing_order(
    *,
    total_display: float,
    display_currency: str,
    description: str,
    return_url: str,
    cancel_url: str,
    custom_id: str | None = None,
) -> dict[str, Any]:
    """One-time CAPTURE order using TESTING_PAYPAL_* credentials only."""
    if not testing_paypal_configured():
        raise RuntimeError(
            "Testing PayPal is not configured. Set TESTING_PAYPAL_CLIENT_ID and "
            "TESTING_PAYPAL_CLIENT_SECRET."
        )
    creds = testing_paypal_creds()
    charge_currency, charge_amount = convert_for_testing_paypal(total_display, display_currency)
    amount_value = f"{float(charge_amount):.2f}"

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
            "brand_name": "UZ PayPal Testing",
            "locale": "en-US",
            "landing_page": "NO_PREFERENCE",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "PAY_NOW",
            "return_url": return_url,
            "cancel_url": cancel_url,
        },
    }

    token = _testing_access_token(
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        env=creds["env"],
    )
    response = _http.post(
        f"{_testing_api_base(creds['env'])}/v2/checkout/orders",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(_paypal_error_detail(response, "Unable to create PayPal order"))

    body = response.json()
    order_id = body.get("id")
    if not order_id:
        raise RuntimeError("PayPal did not return an order id")

    return {
        "order_id": order_id,
        "charge_currency": charge_currency,
        "charge_amount": charge_amount,
        "display_amount": format_display_amount(total_display, display_currency),
        "approve_url": approve_link_from_order(body),
    }


def capture_testing_order(order_id: str) -> dict[str, Any]:
    if not testing_paypal_configured():
        raise RuntimeError(
            "Testing PayPal is not configured. Set TESTING_PAYPAL_CLIENT_ID and "
            "TESTING_PAYPAL_CLIENT_SECRET."
        )
    creds = testing_paypal_creds()
    token = _testing_access_token(
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        env=creds["env"],
    )
    response = _http.post(
        f"{_testing_api_base(creds['env'])}/v2/checkout/orders/{order_id}/capture",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # Return full capture representation so we can require COMPLETED (instant settle).
            "Prefer": "return=representation",
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(_paypal_error_detail(response, "Unable to capture PayPal payment"))

    raw = response.json()
    # PayPal may return HTTP 200 with an error payload (or COMPLETED order + DECLINED capture).
    issue = _capture_failure_detail(raw)
    if issue:
        raise RuntimeError(issue)

    order_status = str(raw.get("status") or "").upper()
    capture_id = None
    transaction_id = None
    capture_status = None
    try:
        units = raw.get("purchase_units") or []
        if units and isinstance(units[0], dict):
            payments = units[0].get("payments") or {}
            captures = payments.get("captures") or []
            if captures and isinstance(captures[0], dict):
                first = captures[0]
                capture_id = first.get("id")
                transaction_id = first.get("id")
                capture_status = str(first.get("status") or "").upper() or None
    except Exception:
        logger.debug("Unable to parse capture ids", exc_info=True)

    # Instant settle only: COMPLETED capture means funds moved (not auth/hold/scheduled).
    # PENDING = under review — not yet reflected as available in the merchant account.
    ok_capture = capture_status == "COMPLETED"
    ok_order = order_status in {"COMPLETED", "CAPTURED"}
    if capture_status == "PENDING":
        raise RuntimeError(
            "Payment is pending PayPal review and is not settled yet. "
            "Check Activity in PayPal — it is not a completed instant payment."
        )
    if not (ok_order and ok_capture and capture_id):
        detail = (
            f"Payment not completed (order={order_status or 'unknown'}, "
            f"capture={capture_status or 'missing'})"
        )
        raise RuntimeError(detail)

    return {
        "order_id": order_id,
        "status": capture_status or order_status,
        "capture_id": capture_id,
        "transaction_id": transaction_id,
        "raw": raw,
    }


def _capture_failure_detail(raw: dict[str, Any]) -> str | None:
    """Return a human error if PayPal body indicates decline / refusal despite HTTP 2xx."""
    name = str(raw.get("name") or "").upper()
    if name in {
        "INSTRUMENT_DECLINED",
        "UNPROCESSABLE_ENTITY",
        "TRANSACTION_REFUSED",
        "PAYER_CANNOT_PAY",
        "CREDIT_CARD_CVV_CHECK_FAILED",
        "CREDIT_CARD_REFUSED",
    }:
        return _format_paypal_issue_body(raw) or name.replace("_", " ").title()

    details = raw.get("details")
    if isinstance(details, list):
        for item in details:
            if not isinstance(item, dict):
                continue
            issue = str(item.get("issue") or item.get("issue_code") or "").upper()
            if issue in {
                "INSTRUMENT_DECLINED",
                "TRANSACTION_REFUSED",
                "PAYER_CANNOT_PAY",
                "CARD_EXPIRED",
                "INVALID_ACCOUNT_STATUS",
                "CREDIT_CARD_CVV_CHECK_FAILED",
                "CREDIT_CARD_REFUSED",
            }:
                return (
                    str(item.get("description") or item.get("message") or "")
                    or issue.replace("_", " ").title()
                )

    try:
        units = raw.get("purchase_units") or []
        if units and isinstance(units[0], dict):
            payments = units[0].get("payments") or {}
            captures = payments.get("captures") or []
            if captures and isinstance(captures[0], dict):
                first = captures[0]
                cap_status = str(first.get("status") or "").upper()
                if cap_status in {"DECLINED", "FAILED", "DENIED", "VOIDED"}:
                    reason = ""
                    processor = first.get("processor_response") or {}
                    if isinstance(processor, dict):
                        reason = str(
                            processor.get("response_code")
                            or processor.get("avs_code")
                            or ""
                        )
                    base = f"Card payment {cap_status.lower()}"
                    return f"{base} ({reason})" if reason else base
    except Exception:
        logger.debug("Unable to inspect capture decline status", exc_info=True)

    return None


def _format_paypal_issue_body(raw: dict[str, Any]) -> str:
    message = str(raw.get("message") or "").strip()
    details = raw.get("details")
    if isinstance(details, list) and details:
        parts: list[str] = []
        for item in details:
            if isinstance(item, dict):
                parts.append(
                    str(item.get("description") or item.get("issue") or item)
                )
            else:
                parts.append(str(item))
        joined = "; ".join(p for p in parts if p)
        if joined:
            return f"{message}: {joined}" if message else joined
    return message


def _product_name() -> str:
    return (
        _clean_env("TESTING_PAYPAL_PRODUCT_NAME")
        or "UZ Testing Monthly Donation (Sandbox)"
    )


def ensure_testing_product() -> str:
    global _product_id
    with _product_lock:
        if _product_id:
            return _product_id

    creds = testing_paypal_creds()
    headers = _auth_headers()
    payload = {
        "name": _product_name()[:127],
        "description": "Sandbox-only recurring donation plans for /testing-paypal",
        "type": "SERVICE",
        "category": "CHARITY",
    }
    response = _http.post(
        f"{_testing_api_base(creds['env'])}/v1/catalogs/products",
        json=payload,
        headers=headers,
    )
    if response.status_code >= 400:
        raise RuntimeError(_paypal_error_detail(response, "Unable to create PayPal product"))

    body = response.json()
    product_id = body.get("id")
    if not product_id:
        raise RuntimeError("PayPal did not return a product id")

    with _product_lock:
        _product_id = str(product_id)
        return _product_id


def ensure_testing_plan(
    *,
    total_display: float,
    display_currency: str,
) -> dict[str, Any]:
    """Create or reuse a Billing Plan: instant setup_fee + monthly REGULAR schedule.

    Hybrid billing (testing-paypal only):
    - setup_fee = first gift, charged immediately when the subscriber approves
    - REGULAR monthly cycle starts ~1 month later (see create_testing_subscription start_time)
    """
    charge_currency, charge_amount = convert_for_testing_paypal(total_display, display_currency)
    amount_value = f"{float(charge_amount):.2f}"
    # Bust older plans that had no setup_fee (those only scheduled, no instant first charge).
    cache_key = (charge_currency.upper(), amount_value, "hybrid-setup-fee-v1")

    with _plan_lock:
        cached = _plan_cache.get(cache_key)
        if cached:
            return {
                "product_id": cached["product_id"],
                "plan_id": cached["plan_id"],
                "charge_currency": charge_currency,
                "charge_amount": charge_amount,
                "display_amount": format_display_amount(total_display, display_currency),
                "reused": True,
                "billing_mode": "hybrid",
            }

    product_id = ensure_testing_product()
    creds = testing_paypal_creds()
    headers = _auth_headers()
    plan_payload = {
        "product_id": product_id,
        "name": f"Testing hybrid monthly {amount_value} {charge_currency}"[:127],
        "description": (
            "Instant first charge (setup fee) + scheduled monthly renewals for /testing-paypal"
        ),
        "status": "ACTIVE",
        "billing_cycles": [
            {
                "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                "tenure_type": "REGULAR",
                "sequence": 1,
                "total_cycles": 0,
                "pricing_scheme": {
                    "fixed_price": {
                        "value": amount_value,
                        "currency_code": charge_currency.upper(),
                    }
                },
            }
        ],
        "payment_preferences": {
            "auto_bill_outstanding": True,
            # Instant first payment into the merchant account on subscribe.
            "setup_fee": {
                "value": amount_value,
                "currency_code": charge_currency.upper(),
            },
            # If the instant charge fails, do not leave an active subscription.
            "setup_fee_failure_action": "CANCEL",
            "payment_failure_threshold": 3,
        },
    }

    response = _http.post(
        f"{_testing_api_base(creds['env'])}/v1/billing/plans",
        json=plan_payload,
        headers={**headers, "Prefer": "return=representation"},
    )
    if response.status_code >= 400:
        raise RuntimeError(_paypal_error_detail(response, "Unable to create PayPal billing plan"))

    body = response.json()
    plan_id = body.get("id")
    if not plan_id:
        raise RuntimeError("PayPal did not return a plan id")

    status = str(body.get("status") or "").upper()
    if status == "CREATED":
        activate = _http.post(
            f"{_testing_api_base(creds['env'])}/v1/billing/plans/{plan_id}/activate",
            headers=headers,
        )
        if activate.status_code >= 400:
            raise RuntimeError(
                _paypal_error_detail(activate, "Unable to activate PayPal billing plan")
            )

    with _plan_lock:
        _plan_cache[cache_key] = {
            "product_id": product_id,
            "plan_id": str(plan_id),
        }

    return {
        "product_id": product_id,
        "plan_id": str(plan_id),
        "charge_currency": charge_currency,
        "charge_amount": charge_amount,
        "display_amount": format_display_amount(total_display, display_currency),
        "reused": False,
        "billing_mode": "hybrid",
    }


def subscription_regular_start_time_iso() -> str:
    """UTC start for the first REGULAR cycle (~1 month out). Setup fee is charged on approve."""
    start = datetime.now(timezone.utc) + timedelta(days=32)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def create_testing_subscription(
    *,
    plan_id: str,
    return_url: str,
    cancel_url: str,
    custom_id: str | None = None,
    subscriber: dict[str, Any] | None = None,
    start_time: str | None = None,
) -> dict[str, Any]:
    creds = testing_paypal_creds()
    headers = _auth_headers()
    # Defer REGULAR billing so the setup_fee is the only instant charge (no double bill).
    regular_start = (start_time or "").strip() or subscription_regular_start_time_iso()
    payload: dict[str, Any] = {
        "plan_id": plan_id,
        "start_time": regular_start,
        "application_context": {
            "brand_name": "UZ PayPal Testing",
            "locale": "en-US",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "SUBSCRIBE_NOW",
            "return_url": return_url,
            "cancel_url": cancel_url,
        },
    }
    if custom_id:
        payload["custom_id"] = custom_id[:127]
    if subscriber:
        payload["subscriber"] = subscriber

    response = _http.post(
        f"{_testing_api_base(creds['env'])}/v1/billing/subscriptions",
        json=payload,
        headers={**headers, "Prefer": "return=representation"},
    )
    if response.status_code >= 400:
        raise RuntimeError(
            _paypal_error_detail(response, "Unable to create PayPal subscription")
        )

    body = response.json()
    subscription_id = body.get("id")
    if not subscription_id:
        raise RuntimeError("PayPal did not return a subscription id")

    approve_url = None
    for link in body.get("links") or []:
        if isinstance(link, dict) and str(link.get("rel") or "").lower() == "approve":
            approve_url = link.get("href")
            break

    return {
        "subscription_id": str(subscription_id),
        "status": str(body.get("status") or ""),
        "plan_id": plan_id,
        "approve_url": approve_url,
        "start_time": regular_start,
        "billing_mode": "hybrid",
        "raw": body,
    }


def get_testing_subscription(subscription_id: str) -> dict[str, Any]:
    creds = testing_paypal_creds()
    headers = _auth_headers()
    response = _http.get(
        f"{_testing_api_base(creds['env'])}/v1/billing/subscriptions/{subscription_id}",
        headers=headers,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            _paypal_error_detail(response, "Unable to fetch PayPal subscription")
        )

    body = response.json()
    billing_info = body.get("billing_info") if isinstance(body.get("billing_info"), dict) else {}
    next_billing = billing_info.get("next_billing_time") if isinstance(billing_info, dict) else None
    last_payment = None
    if isinstance(billing_info, dict):
        lp = billing_info.get("last_payment")
        if isinstance(lp, dict):
            last_payment = lp

    return {
        "subscription_id": str(body.get("id") or subscription_id),
        "status": str(body.get("status") or ""),
        "plan_id": str(body.get("plan_id") or ""),
        "next_billing_time": next_billing,
        "last_payment": last_payment,
        "raw": body,
    }
