from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from typing import Sequence
from urllib.parse import quote, urlencode

import httpx

logger = logging.getLogger(__name__)

from currency import convert_for_paypal, format_display_amount

# Reuse TCP/TLS to PayPal so token + order create are not cold handshakes every click.
_http = httpx.Client(timeout=15.0)
_token_lock = threading.Lock()
# client_id -> (access_token, expires_at_epoch)
_token_cache: dict[str, tuple[str, float]] = {}
# client_id -> "live" | "sandbox" (detected from which OAuth host accepts the keys)
_cred_env_cache: dict[str, str] = {}


def _clean_env(name: str, fallback: str = "") -> str:
    raw = os.getenv(name, fallback) or fallback
    return raw.strip().strip('"').strip("'")


def paypal_client_id() -> str:
    return _clean_env("PAYPAL_CLIENT_ID") or _clean_env("NEXT_PUBLIC_PAYPAL_CLIENT_ID")


def paypal_client_secret() -> str:
    return _clean_env("PAYPAL_CLIENT_SECRET")


def paypal_env() -> str:
    explicit = _clean_env("PAYPAL_ENV").lower()
    if explicit:
        return explicit

    frontend = _clean_env("FRONTEND_URL").lower()
    if frontend and "localhost" not in frontend and "127.0.0.1" not in frontend:
        return "live"

    return "sandbox"


def paypal_configured() -> bool:
    return bool(paypal_client_id() and paypal_client_secret())


def paypal_connect_available() -> bool:
    return bool(paypal_client_id())


def _api_base_for_env(env: str) -> str:
    if (env or "").strip().lower() == "live":
        return "https://api-m.paypal.com"
    return "https://api-m.sandbox.paypal.com"


def paypal_api_base() -> str:
    return _api_base_for_env(paypal_env())


def paypal_web_base() -> str:
    if paypal_env() == "live":
        return "https://www.paypal.com"
    return "https://www.sandbox.paypal.com"


def set_paypal_credentials_env(client_id: str, env: str) -> None:
    """Pin API host for known merchant/testing keys (live|sandbox)."""
    cid = (client_id or "").strip()
    mode = (env or "").strip().lower()
    if cid and mode in {"live", "sandbox"}:
        _cred_env_cache[cid] = mode


def detect_paypal_credentials_env(client_id: str, client_secret: str) -> str:
    """Return 'live' or 'sandbox' for keys by trying OAuth against both hosts."""
    cid = (client_id or "").strip()
    secret = (client_secret or "").strip()
    if not cid or not secret:
        raise RuntimeError("PayPal is not configured")

    cached = _cred_env_cache.get(cid)
    if cached in ("live", "sandbox"):
        return cached

    # Prefer platform env first (faster when keys match), then the other.
    preferred = paypal_env()
    order = [preferred, "sandbox" if preferred == "live" else "live"]
    last_detail = "PayPal credentials rejected"
    for env in order:
        try:
            response = _http.post(
                f"{_api_base_for_env(env)}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(cid, secret),
                headers={"Accept": "application/json"},
            )
            if response.status_code < 400 and response.json().get("access_token"):
                _cred_env_cache[cid] = env
                return env
            try:
                last_detail = response.json().get("error_description") or response.text or last_detail
            except Exception:
                last_detail = response.text or last_detail
        except httpx.HTTPError:
            continue
    raise RuntimeError(str(last_detail))


def paypal_api_base_for(
    client_id: str | None = None,
    client_secret: str | None = None,
) -> str:
    """API host for these credentials (auto-detect when merchant keys are passed)."""
    cid = (client_id or "").strip()
    secret = (client_secret or "").strip()
    if cid and secret:
        try:
            return _api_base_for_env(detect_paypal_credentials_env(cid, secret))
        except RuntimeError:
            pass
    return paypal_api_base()


def _paypal_access_token(
    client_id: str | None = None,
    client_secret: str | None = None,
) -> str:
    cid = (client_id or "").strip() or paypal_client_id()
    secret = (client_secret or "").strip() or paypal_client_secret()
    if not cid or not secret:
        raise RuntimeError("PayPal is not configured")

    now = time.time()
    with _token_lock:
        cached = _token_cache.get(cid)
        if cached and cached[1] > now + 60:
            return cached[0]

    api_base = paypal_api_base_for(cid, secret)
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
        raise RuntimeError(detail or "PayPal credentials rejected") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("Unable to reach PayPal") from exc

    body = response.json()
    token = body.get("access_token")
    if not token:
        raise RuntimeError("PayPal did not return an access token")
    expires_in = int(body.get("expires_in") or 28800)
    # Refresh a couple minutes early so checkout never uses an expired token.
    expires_at = now + max(120, expires_in - 120)
    with _token_lock:
        _token_cache[cid] = (str(token), expires_at)
    # Remember which host accepted these keys.
    if api_base.endswith("paypal.com") and "sandbox" not in api_base:
        _cred_env_cache[cid] = "live"
    else:
        _cred_env_cache[cid] = "sandbox"
    return str(token)


def warm_paypal_access_token(
    client_id: str | None = None,
    client_secret: str | None = None,
) -> None:
    """Prefetch/cache OAuth token so the donor click only creates the order."""
    try:
        _paypal_access_token(client_id=client_id, client_secret=client_secret)
    except Exception:
        logger.debug("PayPal token warm skipped", exc_info=True)


def verify_paypal_credentials(client_id: str, client_secret: str) -> bool:
    try:
        detect_paypal_credentials_env(client_id, client_secret)
        _paypal_access_token(client_id=client_id, client_secret=client_secret)
        return True
    except Exception:
        return False


def probe_paypal_subscriptions_capability(
    client_id: str,
    client_secret: str,
) -> dict[str, object]:
    """
    Check whether these REST keys can use Catalog Products / Billing Plans.
    Monthly checkout requires this; one-time Orders can work without it.
    """
    cid = (client_id or "").strip()
    secret = (client_secret or "").strip()
    token = _paypal_access_token(client_id=cid, client_secret=secret)
    api_base = paypal_api_base_for(cid, secret)
    response = _http.get(
        f"{api_base}/v1/catalogs/products",
        params={"page_size": 1, "page": 1, "total_required": "false"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    if response.status_code < 400:
        return {
            "ok": True,
            "api_env": _cred_env_cache.get(cid) or paypal_env(),
        }
    detail = _paypal_error_detail(response, "Subscriptions API not available for these keys")
    return {
        "ok": False,
        "api_env": _cred_env_cache.get(cid) or paypal_env(),
        "detail": detail,
    }


def client_id_hint(client_id: str) -> str:
    value = (client_id or "").strip()
    if len(value) <= 8:
        return value
    return f"{value[:4]}…{value[-4:]}"


def approve_link_from_order(order: dict[str, object]) -> str | None:
    links = order.get("links")
    if not isinstance(links, list):
        return None
    for link in links:
        if not isinstance(link, dict):
            continue
        if str(link.get("rel") or "").lower() == "approve" and link.get("href"):
            return str(link["href"])
    return None


def build_paypal_oauth_url(*, state: str, redirect_uri: str) -> str:
    client_id = paypal_client_id()
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": "openid profile email https://uri.paypal.com/services/paypalattributes",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{paypal_web_base()}/signin/authorize?{urlencode(params)}"


def create_paypal_partner_onboarding_url(*, state: str, return_url: str) -> str:
    token = _paypal_access_token()
    payload = {
        "tracking_id": state[:127],
        "operations": [
            {
                "operation": "API_INTEGRATION",
                "api_integration_preference": {
                    "rest_api_integration": {
                        "integration_method": "PAYPAL",
                        "integration_type": "THIRD_PARTY",
                        "third_party_details": {
                            "features": [
                                "PAYMENT",
                                "REFUND",
                                "PARTNER_FEE",
                                "ACCESS_MERCHANT_INFORMATION",
                            ],
                        },
                    }
                },
            }
        ],
        "products": ["PPCP"],
        "partner_config_override": {
            "return_url": return_url,
            "return_url_description": "Return to your donation platform after PayPal onboarding",
        },
        "legal_consents": [
            {
                "type": "SHARE_DATA_CONSENT",
                "granted": True,
            }
        ],
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    bn_code = os.getenv("PAYPAL_BN_CODE", "")
    if bn_code:
        headers["PayPal-Partner-Attribution-Id"] = bn_code

    try:
        response = httpx.post(
            f"{paypal_api_base()}/v2/customer/partner-referrals",
            json=payload,
            headers=headers,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError("Unable to reach PayPal partner API") from exc

    if response.status_code >= 400:
        detail = response.text
        try:
            detail = response.json().get("message", detail)
        except Exception:
            pass
        raise RuntimeError(detail or "Unable to start PayPal onboarding")

    body = response.json()
    for link in body.get("links", []):
        if link.get("rel") == "action_url" and link.get("href"):
            return link["href"]

    raise RuntimeError("PayPal did not return an onboarding link")


def _partner_connect_blocked(message: str) -> bool:
    lowered = message.lower()
    return any(
        phrase in lowered
        for phrase in (
            "insufficient permissions",
            "not authorized",
            "not authorised",
            "permission denied",
            "forbidden",
        )
    )


def build_paypal_connect_url(*, state: str, redirect_uri: str, frontend_url: str) -> str:
    """Try PayPal partner onboarding; fall back to business-email connect for standard REST apps."""
    if paypal_configured():
        partner_error: str | None = None
        return_url = f"{redirect_uri}?state={quote(state, safe='')}"
        try:
            return create_paypal_partner_onboarding_url(state=state, return_url=return_url)
        except Exception as exc:
            partner_error = str(exc).strip() or "unknown error"
            logger.warning("PayPal partner onboarding failed: %s", partner_error)

        use_oauth = os.getenv("PAYPAL_CONNECT_USE_OAUTH", "").lower() in ("1", "true", "yes")
        if use_oauth and not _partner_connect_blocked(partner_error or ""):
            return build_paypal_oauth_url(state=state, redirect_uri=redirect_uri)

        logger.info("Using PayPal business-email connect fallback")
        return build_paypal_hosted_connect_url(state=state, frontend_url=frontend_url)

    if paypal_client_id():
        raise RuntimeError(
            "PayPal client secret is missing on the backend. Add PAYPAL_CLIENT_SECRET in Railway."
        )

    return build_paypal_hosted_connect_url(state=state, frontend_url=frontend_url)


def build_paypal_hosted_connect_url(*, state: str, frontend_url: str) -> str:
    return f"{frontend_url.rstrip('/')}/connect/paypal?state={quote(state, safe='')}"


def exchange_paypal_code(code: str, redirect_uri: str) -> tuple[str, str | None]:
    client_id = paypal_client_id()
    client_secret = paypal_client_secret()
    if not client_id or not client_secret:
        raise RuntimeError("PayPal is not configured")

    token_response = httpx.post(
        f"{paypal_api_base()}/v1/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        auth=(client_id, client_secret),
        headers={"Accept": "application/json"},
        timeout=30.0,
    )
    if token_response.status_code >= 400:
        raise RuntimeError("PayPal authorization failed")

    token_body = token_response.json()
    access_token = token_body.get("access_token")
    merchant_id = token_body.get("payer_id") or token_body.get("user_id") or token_body.get("sub")
    email: str | None = None

    if access_token:
        user_response = httpx.get(
            f"{paypal_api_base()}/v1/identity/oauth2/userinfo?schema=paypalv1.1",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
        if user_response.status_code < 400:
            profile = user_response.json()
            merchant_id = profile.get("payer_id") or profile.get("user_id") or merchant_id
            email = profile.get("email")
            if not email and isinstance(profile.get("emails"), list) and profile["emails"]:
                first = profile["emails"][0]
                if isinstance(first, dict):
                    email = first.get("value")

    if not merchant_id:
        merchant_id = "connected"

    return merchant_id, email


def create_paypal_client_token(
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> str:
    """Client token for Advanced Card Fields / Apple Pay / Google Pay JS SDK."""
    token = _paypal_access_token(client_id=client_id, client_secret=client_secret)
    response = _http.post(
        f"{paypal_api_base_for(client_id, client_secret)}/v1/identity/generate-token",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept-Language": "en_US",
        },
        json={},
    )
    if response.status_code >= 400:
        detail = response.text
        try:
            body = response.json()
            detail = body.get("error_description") or body.get("message") or detail
        except Exception:
            pass
        raise RuntimeError(detail or "Unable to generate PayPal client token")
    body = response.json()
    client_token = body.get("client_token")
    if not client_token:
        raise RuntimeError("PayPal did not return a client_token")
    return str(client_token)


def create_paypal_order(
    *,
    total_display: float,
    display_currency: str,
    description: str,
    return_url: str,
    cancel_url: str,
    custom_id: str | None = None,
    payee_email: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, object]:
    charge_currency, charge_amount = convert_for_paypal(total_display, display_currency)
    amount_value = f"{charge_amount:.2f}"

    purchase_unit: dict[str, object] = {
        "amount": {
            "currency_code": charge_currency,
            "value": amount_value,
        },
        "description": description[:127],
    }
    if custom_id:
        purchase_unit["custom_id"] = custom_id[:127]
    if payee_email:
        purchase_unit["payee"] = {"email_address": payee_email}

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

    token = _paypal_access_token(client_id=client_id, client_secret=client_secret)
    response = _http.post(
        f"{paypal_api_base_for(client_id, client_secret)}/v2/checkout/orders",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
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
        "approve_url": approve_link_from_order(body),
        "raw": body,
    }


def capture_paypal_order(
    order_id: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, object]:
    token = _paypal_access_token(client_id=client_id, client_secret=client_secret)
    response = _http.post(
        f"{paypal_api_base_for(client_id, client_secret)}/v2/checkout/orders/{order_id}/capture",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    if response.status_code >= 400:
        detail = response.text
        try:
            body = response.json()
            detail = body.get("message", detail)
            details = body.get("details")
            if isinstance(details, list) and details:
                issue = str(details[0].get("issue") or details[0].get("description") or "").strip()
                if issue:
                    detail = issue if not detail else f"{detail}: {issue}"
        except Exception:
            pass
        raise RuntimeError(detail or "Unable to capture PayPal payment")

    body = response.json()
    order_status = str(body.get("status") or "").upper()
    capture_statuses: list[str] = []
    for unit in body.get("purchase_units") or []:
        if not isinstance(unit, dict):
            continue
        payments = unit.get("payments") if isinstance(unit.get("payments"), dict) else {}
        for cap in payments.get("captures") or []:
            if isinstance(cap, dict) and cap.get("status"):
                capture_statuses.append(str(cap.get("status")).upper())

    # Prefer nested capture status — order can look COMPLETED while a capture was DECLINED.
    if capture_statuses:
        if not any(status == "COMPLETED" for status in capture_statuses):
            bad = ", ".join(sorted(set(capture_statuses)))
            raise RuntimeError(f"PayPal capture not completed (status={bad})")
    elif order_status != "COMPLETED":
        raise RuntimeError(f"PayPal payment was not completed (status={order_status or 'unknown'})")

    return body


_product_lock = threading.Lock()
_plan_lock = threading.Lock()
# client_id -> product_id
_product_cache: dict[str, str] = {}
# (client_id, currency, amount) -> {product_id, plan_id}
_plan_cache: dict[tuple[str, str, str], dict[str, str]] = {}


def _paypal_error_detail(response: httpx.Response, fallback: str) -> str:
    detail = response.text
    try:
        body = response.json()
        message = str(body.get("message") or body.get("error_description") or "").strip()
        details = body.get("details")
        detail_parts: list[str] = []
        if isinstance(details, list):
            for item in details:
                if isinstance(item, dict):
                    part = str(item.get("description") or item.get("issue") or "").strip()
                    if part:
                        detail_parts.append(part)
                else:
                    detail_parts.append(str(item))
        if message and detail_parts:
            detail = f"{message}: {'; '.join(detail_parts)}"
        elif message:
            detail = message
        elif detail_parts:
            detail = "; ".join(detail_parts)
        elif details:
            detail = str(details)
        lower = detail.lower()
        if "insufficient permissions" in lower or "not_authorized" in lower or "permission_denied" in lower:
            detail = (
                f"{detail} "
                "Monthly PayPal needs Subscriptions on this REST app: "
                "developer.paypal.com → Apps → your app → Accept payments → Advanced options → "
                "enable Billing agreements + Future payments (and Subscriptions if shown), Save, "
                "wait a few minutes, then re-attach the same Client ID/Secret. "
                "One-time checkout can work without this; monthly cannot."
            )
    except Exception:
        pass
    return str(detail or fallback)


def subscription_regular_start_time_iso() -> str:
    from datetime import datetime, timedelta, timezone

    start = datetime.now(timezone.utc) + timedelta(days=32)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_paypal_product(
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> str:
    cid = (client_id or "").strip() or paypal_client_id()
    with _product_lock:
        cached = _product_cache.get(cid)
        if cached:
            return cached

    token = _paypal_access_token(client_id=client_id, client_secret=client_secret)
    api_base = paypal_api_base_for(client_id, client_secret)
    response = _http.post(
        f"{api_base}/v1/catalogs/products",
        json={
            "name": "Monthly Donation",
            "description": "Recurring donation via PayPal checkout processor",
            "type": "SERVICE",
            # SOFTWARE is widely allowed; CHARITY can be restricted on some merchant apps.
            "category": "SOFTWARE",
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(_paypal_error_detail(response, "Unable to create PayPal product"))
    product_id = response.json().get("id")
    if not product_id:
        raise RuntimeError("PayPal did not return a product id")
    with _product_lock:
        _product_cache[cid] = str(product_id)
    return str(product_id)


def ensure_paypal_plan(
    *,
    total_display: float,
    display_currency: str,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, object]:
    """Hybrid plan: setup_fee charges now; REGULAR monthly starts ~1 month later."""
    charge_currency, charge_amount = convert_for_paypal(total_display, display_currency)
    amount_value = f"{float(charge_amount):.2f}"
    cid = (client_id or "").strip() or paypal_client_id()
    cache_key = (cid, charge_currency.upper(), amount_value)

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

    product_id = ensure_paypal_product(client_id=client_id, client_secret=client_secret)
    token = _paypal_access_token(client_id=client_id, client_secret=client_secret)
    api_base = paypal_api_base_for(client_id, client_secret)
    plan_payload = {
        "product_id": product_id,
        "name": f"Monthly donation {amount_value} {charge_currency}"[:127],
        "description": "Instant first charge + scheduled monthly renewals",
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
            "setup_fee": {
                "value": amount_value,
                "currency_code": charge_currency.upper(),
            },
            "setup_fee_failure_action": "CANCEL",
            "payment_failure_threshold": 3,
        },
    }
    response = _http.post(
        f"{api_base}/v1/billing/plans",
        json=plan_payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
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
            f"{api_base}/v1/billing/plans/{plan_id}/activate",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        if activate.status_code >= 400:
            raise RuntimeError(_paypal_error_detail(activate, "Unable to activate PayPal billing plan"))

    with _plan_lock:
        _plan_cache[cache_key] = {"product_id": product_id, "plan_id": str(plan_id)}

    return {
        "product_id": product_id,
        "plan_id": str(plan_id),
        "charge_currency": charge_currency,
        "charge_amount": charge_amount,
        "display_amount": format_display_amount(total_display, display_currency),
        "reused": False,
        "billing_mode": "hybrid",
    }


def create_paypal_subscription(
    *,
    plan_id: str,
    return_url: str,
    cancel_url: str,
    custom_id: str | None = None,
    subscriber: dict[str, object] | None = None,
    start_time: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, object]:
    token = _paypal_access_token(client_id=client_id, client_secret=client_secret)
    regular_start = (start_time or "").strip() or subscription_regular_start_time_iso()
    payload: dict[str, object] = {
        "plan_id": plan_id,
        "start_time": regular_start,
        "application_context": {
            "brand_name": "Donation",
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
        f"{paypal_api_base_for(client_id, client_secret)}/v1/billing/subscriptions",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
    )
    if response.status_code >= 400:
        raise RuntimeError(_paypal_error_detail(response, "Unable to create PayPal subscription"))
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


def get_paypal_subscription(
    subscription_id: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, object]:
    token = _paypal_access_token(client_id=client_id, client_secret=client_secret)
    response = _http.get(
        f"{paypal_api_base_for(client_id, client_secret)}/v1/billing/subscriptions/{subscription_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    if response.status_code >= 400:
        raise RuntimeError(_paypal_error_detail(response, "Unable to fetch PayPal subscription"))
    body = response.json()
    return {
        "subscription_id": str(body.get("id") or subscription_id),
        "status": str(body.get("status") or ""),
        "plan_id": str((body.get("plan_id") or "")),
        "next_billing_time": (body.get("billing_info") or {}).get("next_billing_time")
        if isinstance(body.get("billing_info"), dict)
        else None,
        "raw": body,
    }


def _b64url_json(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _paypal_auth_assertion(client_id: str, payer_id: str) -> str:
    """PayPal-Auth-Assertion for wallet-domains (iss = REST client id, payer_id = merchant)."""
    header = _b64url_json({"alg": "none"})
    body = _b64url_json({"iss": client_id, "payer_id": payer_id})
    return f"{header}.{body}."


def _resolve_paypal_payer_id(
    *,
    client_id: str | None,
    client_secret: str | None,
    preferred_merchant_id: str | None = None,
) -> str | None:
    preferred = (preferred_merchant_id or "").strip()
    if preferred and not preferred.startswith("keys:") and "@" not in preferred:
        return preferred

    cid = (client_id or "").strip()
    secret = (client_secret or "").strip()
    if not cid or not secret:
        return None

    token = _paypal_access_token(client_id=cid, client_secret=secret)
    api_base = paypal_api_base_for(cid, secret)
    try:
        response = _http.get(
            f"{api_base}/v1/identity/oauth2/userinfo?schema=paypalv1.1",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if response.status_code < 400:
            body = response.json()
            payer = str(body.get("payer_id") or body.get("user_id") or "").strip()
            if payer:
                return payer
    except Exception:
        logger.debug("PayPal payer_id lookup skipped", exc_info=True)
    return None


def register_paypal_apple_pay_domain(
    domain: str,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    merchant_id: str | None = None,
) -> dict[str, object]:
    """
    Register a web domain for Apple Pay with PayPal (wallet-domains API).
    Required before native ApplePaySession merchant validation succeeds.

    Direct REST apps (org/platform API keys) usually succeed without
    PayPal-Auth-Assertion. Multiparty/partner flows need the assertion with the
    merchant payer_id. Wrong assertion → "Subject Authentication failed".
    """
    host = (domain or "").strip().lower().split(":")[0].strip(".")
    if not host or host in {"localhost", "127.0.0.1"}:
        raise RuntimeError("Apple Pay domain registration requires a public HTTPS hostname")

    cid = (client_id or "").strip() or paypal_client_id()
    secret = (client_secret or "").strip() or paypal_client_secret()
    token = _paypal_access_token(client_id=cid, client_secret=secret)
    api_base = paypal_api_base_for(cid, secret)
    payer_id = _resolve_paypal_payer_id(
        client_id=cid,
        client_secret=secret,
        preferred_merchant_id=merchant_id,
    )

    def _post(headers: dict[str, str]) -> httpx.Response:
        return _http.post(
            f"{api_base}/v1/customer/wallet-domains",
            headers=headers,
            json={
                "provider_type": "APPLE_PAY",
                "domain": {"name": host},
            },
        )

    base_headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    attempts: list[dict[str, str]] = [dict(base_headers)]
    # Prefer direct (no assertion) first — org API keys are first-party apps.
    # Then retry with assertion when we have a real merchant payer id.
    if cid and payer_id:
        with_assertion = dict(base_headers)
        with_assertion["PayPal-Auth-Assertion"] = _paypal_auth_assertion(cid, payer_id)
        attempts.append(with_assertion)

    last_response: httpx.Response | None = None
    try:
        for headers in attempts:
            response = _post(headers)
            last_response = response
            if response.status_code in {200, 201, 204}:
                return {
                    "domain": host,
                    "registered": True,
                    "created": True,
                    "client_id_hint": cid[:12] if cid else "",
                    "payer_id": payer_id or "",
                    "used_auth_assertion": "PayPal-Auth-Assertion" in headers,
                }
            if response.status_code in {409, 422}:
                detail = ""
                try:
                    detail = json.dumps(response.json())
                except Exception:
                    detail = response.text or ""
                lowered = detail.lower()
                if any(
                    phrase in lowered
                    for phrase in ("already", "exist", "duplicate", "registered", "conflict")
                ):
                    return {
                        "domain": host,
                        "registered": True,
                        "created": False,
                        "client_id_hint": cid[:12] if cid else "",
                        "payer_id": payer_id or "",
                        "used_auth_assertion": "PayPal-Auth-Assertion" in headers,
                    }
            # Subject Authentication failed → try next strategy (with/without assertion)
            body_text = (response.text or "").lower()
            if "subject authentication" in body_text or response.status_code in {401, 403}:
                continue
            break
    except httpx.HTTPError as exc:
        raise RuntimeError("Unable to reach PayPal to register Apple Pay domain") from exc

    response = last_response
    if response is None:
        raise RuntimeError("Unable to register Apple Pay domain")
    if response.status_code >= 400:
        hint = (
            f" Register '{host}' under PayPal Developer Dashboard → Live app → Features → "
            "Apple Pay → Manage → Add Domain (association file is already hosted on this site)."
        )
        detail = _paypal_error_detail(response, "Unable to register Apple Pay domain")
        raise RuntimeError(f"{detail}{hint}")
    return {
        "domain": host,
        "registered": True,
        "created": False,
        "client_id_hint": cid[:12] if cid else "",
        "payer_id": payer_id or "",
    }