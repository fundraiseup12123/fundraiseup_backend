from __future__ import annotations

import logging
import os
from typing import Sequence
from urllib.parse import quote, urlencode

import httpx

logger = logging.getLogger(__name__)

from currency import convert_for_paypal, format_display_amount


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


def paypal_api_base() -> str:
    if paypal_env() == "live":
        return "https://api-m.paypal.com"
    return "https://api-m.sandbox.paypal.com"


def paypal_web_base() -> str:
    if paypal_env() == "live":
        return "https://www.paypal.com"
    return "https://www.sandbox.paypal.com"


def _paypal_access_token(
    client_id: str | None = None,
    client_secret: str | None = None,
) -> str:
    cid = (client_id or "").strip() or paypal_client_id()
    secret = (client_secret or "").strip() or paypal_client_secret()
    if not cid or not secret:
        raise RuntimeError("PayPal is not configured")

    try:
        response = httpx.post(
            f"{paypal_api_base()}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(cid, secret),
            headers={"Accept": "application/json"},
            timeout=30.0,
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

    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("PayPal did not return an access token")
    return str(token)


def verify_paypal_credentials(client_id: str, client_secret: str) -> bool:
    try:
        _paypal_access_token(client_id=client_id, client_secret=client_secret)
        return True
    except Exception:
        return False


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
