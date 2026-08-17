from __future__ import annotations

import logging

import os
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv

from env_loader import load_app_env

load_app_env()

import stripe
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from currency import (
    PaymentMethodType,
    calculate_total_with_fees,
    charge_currency,
    conversion_note,
    convert_for_charge,
    estimate_processing_fee,
    format_display_amount,
    from_stripe_amount,
    supports_paypal,
    paypal_available,
    to_stripe_amount,
)
from supabase_client import get_donation_by_payment_intent, insert_donation, list_donations, supabase_enabled

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
if not stripe.api_key:
    raise RuntimeError("STRIPE_SECRET_KEY is not set")

app = FastAPI(title="Sudan Donation API", version="1.0.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

cors_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "https://sudanneedsyou-production.up.railway.app",
]
extra_origins = os.getenv("CORS_ORIGINS", "")
if extra_origins:
    cors_origins.extend(origin.strip() for origin in extra_origins.split(",") if origin.strip())
frontend_url = os.getenv("FRONTEND_URL", "").strip().rstrip("/")
if frontend_url and frontend_url not in cors_origins:
    cors_origins.append(frontend_url)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=r"https://.*\.(up\.railway\.app|vercel\.app)|https://.*\.ngrok-free\.(app|dev)|https://.*\.ngrok\.io",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class DonorDetails(BaseModel):
    # Empty allowed for wallet express (Apple/Google Pay) — real payer filled from billing after pay.
    first_name: str = Field(default="", max_length=80)
    last_name: str = Field(default="", max_length=80)
    email: str = Field(default="", max_length=254)
    phone: str | None = None


class UtmParams(BaseModel):
    source: str | None = None
    medium: str | None = None
    campaign: str | None = None
    term: str | None = None
    content: str | None = None


class DeviceInfo(BaseModel):
    os: str | None = None
    browser: str | None = None
    type: str | None = None
    country: str | None = None
    city: str | None = None
    gender: str | None = None


class CreateCheckoutRequest(BaseModel):
    amount: float = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    frequency: Literal["once", "monthly"] = "once"
    cover_fees: bool = False
    dedicate: bool = False
    honoree_name: str | None = None
    comment: str | None = None
    donor: DonorDetails
    payment_method: PaymentMethodType = "card"
    campaign_id: str | None = None
    checkout_view: Literal["homepage", "popup", "landing"] = "homepage"
    utm: UtmParams | None = None
    device: DeviceInfo | None = None


class SwitchPaymentMethodRequest(BaseModel):
    payment_method: PaymentMethodType
    cover_fees: bool = False


class UpdateCheckoutRequest(BaseModel):
    cover_fees: bool


class RegisterDomainRequest(BaseModel):
    domain: str = Field(min_length=3, max_length=253)
    # Required for Connect direct charges — Apple Pay / Google Pay domains must be
    # registered on the connected account that owns the PaymentIntent.
    stripe_account: str | None = Field(default=None, max_length=255)


class RecordDonationRequest(BaseModel):
    payment_intent_id: str = Field(min_length=3, max_length=255)


class DonationFeedItem(BaseModel):
    id: str
    first_name: str
    last_name: str
    amount: float
    currency: str
    frequency: Literal["once", "monthly"]
    honoree_name: str | None = None
    created_at: str
    country_code: str | None = None
    crypto_amount: float | None = None
    crypto_currency: str | None = None


class DonationFeedResponse(BaseModel):
    donations: list[DonationFeedItem]
    has_more: bool


def _country_code_from_device(device: object) -> str | None:
    if not isinstance(device, dict):
        return None
    raw = (
        device.get("country")
        or device.get("Country")
        or device.get("country_code")
        or device.get("countryCode")
    )
    if raw is None:
        return None
    code = str(raw).strip().upper()
    if len(code) == 2 and code.isalpha():
        return code
    return None


def _feed_item_from_row(row: dict) -> DonationFeedItem:
    crypto_amount = row.get("crypto_amount")
    crypto_currency = row.get("crypto_currency")
    try:
        crypto_amount_f = float(crypto_amount) if crypto_amount is not None else None
    except (TypeError, ValueError):
        crypto_amount_f = None
    crypto_code = str(crypto_currency).upper() if crypto_currency else None
    return DonationFeedItem(
        id=str(row["id"]),
        first_name=row["first_name"],
        last_name=row["last_name"],
        amount=float(row["amount"]),
        currency=str(row["currency"]).upper(),
        frequency=row["frequency"] if row["frequency"] in {"once", "monthly"} else "once",
        honoree_name=row.get("honoree_name"),
        created_at=row["created_at"],
        country_code=_country_code_from_device(row.get("device")),
        crypto_amount=crypto_amount_f,
        crypto_currency=crypto_code,
    )


class WalletDomainResponse(BaseModel):
    domain: str
    registered: bool
    created: bool = False
    google_pay_status: str | None = None
    apple_pay_status: str | None = None


class CheckoutResponse(BaseModel):
    client_secret: str
    payment_intent_id: str | None = None
    subscription_id: str | None = None
    display_amount: str
    base_amount: float
    total_amount: float
    currency: str
    display_currency: str
    charge_currency: str
    charge_amount: float
    conversion_note: str | None = None
    frequency: Literal["once", "monthly"]
    paypal_available: bool
    google_pay_available: bool
    stripe_connect_account: str | None = None


def _is_uuid(value: str) -> bool:
    import re
    return bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value, re.I))


@app.get("/config")
def config() -> dict[str, str | list[str] | bool | None]:
    publishable = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
    from domain_utils import platform_domain_config

    domain_config = platform_domain_config()
    return {
        "publishable_key": publishable,
        "paypal_currencies": sorted({"usd", "eur", "gbp", "aud", "cad"}),
        **domain_config,
    }


@app.post("/wallet/register-domain", response_model=WalletDomainResponse)
def register_wallet_domain(payload: RegisterDomainRequest) -> WalletDomainResponse:
    domain = payload.domain.strip().lower()
    if domain.startswith("http://") or domain.startswith("https://"):
        raise HTTPException(status_code=400, detail="Enter only the domain name, not a full URL.")

    stripe_account = (payload.stripe_account or "").strip() or None

    def _register(account_id: str | None) -> WalletDomainResponse:
        account_kwargs = {"stripe_account": account_id} if account_id else {}

        def _status(item) -> WalletDomainResponse:
            try:
                item = stripe.PaymentMethodDomain.validate(item.id, **account_kwargs)
            except Exception:
                pass
            return WalletDomainResponse(
                domain=domain,
                registered=True,
                google_pay_status=getattr(getattr(item, "google_pay", None), "status", None),
                apple_pay_status=getattr(getattr(item, "apple_pay", None), "status", None),
            )

        existing_domains = stripe.PaymentMethodDomain.list(limit=100, **account_kwargs)
        for item in existing_domains.data:
            if item.domain_name == domain:
                return _status(item)

        created = stripe.PaymentMethodDomain.create(domain_name=domain, **account_kwargs)
        return _status(created)

    try:
        # Always register on the platform account.
        platform_result = _register(None)
        # For Connect direct charges, also register on the connected account.
        if stripe_account:
            connect_result = _register(stripe_account)
            return connect_result
        return platform_result
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=400, detail=str(exc.user_message or exc)) from exc


def _resolve_amounts(amount: float, currency: str, cover_fees: bool) -> tuple[float, float]:
    base = round(amount, 2)
    total = calculate_total_with_fees(base, currency) if cover_fees else base
    return base, total


def _payment_method_types(charge_curr: str, payment_method: PaymentMethodType) -> list[str]:
    methods: list[str] = ["card"]
    if payment_method == "paypal" and supports_paypal(charge_curr):
        methods.append("paypal")
    return methods


def _checkout_metadata(
    payload: CreateCheckoutRequest,
    base_amount: float,
    payment_method: PaymentMethodType,
    *,
    organization_id: str | None = None,
    campaign_id: str | None = None,
    campaign_slug: str | None = None,
    stripe_account: str | None = None,
) -> dict[str, str]:
    meta = {
        "frequency": payload.frequency,
        "dedicate": str(payload.dedicate).lower(),
        "honoree_name": (payload.honoree_name or "")[:500],
        "comment": (payload.comment or "")[:500],
        "base_amount": str(base_amount),
        "cover_fees": str(payload.cover_fees).lower(),
        "campaign": campaign_slug or "sdnemergency",
        "display_currency": payload.currency.upper(),
        "payment_method": payment_method,
        # Stripe PaymentIntents always settle on Stripe, even when the campaign
        # is configured for hybrid Authorize.net (wallets / platform landing).
        "payment_processor": "stripe",
    }
    first_name = _clean_donor_name(payload.donor.first_name)
    last_name = _clean_donor_name(payload.donor.last_name)
    email = _clean_donor_email(payload.donor.email)
    if first_name:
        meta["first_name"] = first_name
    if last_name:
        meta["last_name"] = last_name
    if email:
        meta["email"] = email
    phone = (payload.donor.phone or "").strip()
    if phone:
        meta["phone"] = phone
    if organization_id:
        meta["organization_id"] = organization_id
    if campaign_id:
        meta["campaign_id"] = campaign_id
    if stripe_account:
        meta["stripe_connect_account"] = stripe_account
    meta["checkout_view"] = (
        payload.checkout_view
        if payload.checkout_view in ("homepage", "popup", "landing")
        else "homepage"
    )
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
    if payload.device:
        device_fields = {
            "device_os": payload.device.os,
            "device_browser": payload.device.browser,
            "device_type": payload.device.type,
            "device_country": payload.device.country,
            "device_city": payload.device.city,
            "device_gender": payload.device.gender,
        }
        for key, value in device_fields.items():
            if value:
                meta[key] = str(value)[:120]
    return meta


def _device_from_meta(meta: dict[str, str]) -> dict[str, str]:
    fields = {
        "os": meta.get("device_os"),
        "browser": meta.get("device_browser"),
        "type": meta.get("device_type"),
        "country": meta.get("device_country"),
        "city": meta.get("device_city"),
        "gender": meta.get("device_gender"),
    }
    cleaned = {key: value for key, value in fields.items() if value}
    checkout_view = meta.get("checkout_view")
    cleaned["checkout_view"] = (
        checkout_view if checkout_view in ("homepage", "popup", "landing") else "homepage"
    )
    # This helper only runs for Stripe PaymentIntent rows.
    cleaned["processor"] = "stripe"
    return cleaned


def _utm_from_meta(meta: dict[str, str]) -> dict[str, str] | None:
    fields = {
        "source": meta.get("utm_source"),
        "medium": meta.get("utm_medium"),
        "campaign": meta.get("utm_campaign"),
        "term": meta.get("utm_term"),
        "content": meta.get("utm_content"),
    }
    cleaned = {key: value for key, value in fields.items() if value}
    return cleaned or None


def _create_once_payment_intent(
    *,
    display_currency: str,
    payment_method: PaymentMethodType,
    base_amount: float,
    total_display: float,
    cover_fees: bool,
    customer_id: str,
    metadata: dict[str, str],
    stripe_account: str | None = None,
) -> stripe.PaymentIntent:
    charge_curr = charge_currency(display_currency, payment_method)
    charge_total = convert_for_charge(total_display, display_currency, payment_method)
    stripe_amount = to_stripe_amount(charge_total, charge_curr)

    full_metadata = {
        **metadata,
        "charge_currency": charge_curr.upper(),
        "charge_amount": str(charge_total),
        "total_display": str(total_display),
        "cover_fees": str(cover_fees).lower(),
    }

    create_kwargs: dict = {
        "amount": stripe_amount,
        "currency": charge_curr,
        "customer": customer_id,
        # Do not set receipt_email — donors get confirmation via our emails, not Stripe receipts.
        "metadata": full_metadata,
        "payment_method_types": _payment_method_types(charge_curr, payment_method),
    }
    if stripe_account:
        create_kwargs["stripe_account"] = stripe_account
    return stripe.PaymentIntent.create(**create_kwargs)


def _payment_intent_id_from_invoice(invoice: stripe.Invoice) -> str | None:
    payments = getattr(invoice, "payments", None)
    if payments and getattr(payments, "data", None):
        for entry in payments.data:
            payment = getattr(entry, "payment", None)
            if not payment:
                continue
            payment_intent = getattr(payment, "payment_intent", None)
            if isinstance(payment_intent, str):
                return payment_intent
            if payment_intent and getattr(payment_intent, "id", None):
                return payment_intent.id

    payment_intent = getattr(invoice, "payment_intent", None)
    if isinstance(payment_intent, str):
        return payment_intent
    if payment_intent and getattr(payment_intent, "id", None):
        return payment_intent.id
    return None


def _payment_intent_id_from_client_secret(client_secret: str | None) -> str | None:
    """Parse pi_… from a PaymentIntent client_secret when invoice expansion omits the id."""
    if not client_secret or not isinstance(client_secret, str):
        return None
    if client_secret.startswith("pi_") and "_secret_" in client_secret:
        return client_secret.split("_secret_", 1)[0]
    return None


def _invoice_id_from_payment_intent(payment_intent: Any) -> str | None:
    """Resolve invoice id for subscription PIs (legacy `invoice` or new payment_details)."""
    try:
        invoice_ref = payment_intent["invoice"]
    except (KeyError, TypeError, AttributeError):
        invoice_ref = None
    if isinstance(invoice_ref, str) and invoice_ref:
        return invoice_ref
    if invoice_ref is not None:
        invoice_id = getattr(invoice_ref, "id", None)
        if invoice_id:
            return str(invoice_id)
        if isinstance(invoice_ref, dict) and invoice_ref.get("id"):
            return str(invoice_ref["id"])

    # Newer Stripe invoice-payment PIs omit `invoice` and link via order_reference.
    try:
        details = payment_intent["payment_details"]
    except (KeyError, TypeError, AttributeError):
        details = getattr(payment_intent, "payment_details", None)
    if details is not None:
        if isinstance(details, dict):
            order_ref = details.get("order_reference")
        else:
            order_ref = getattr(details, "order_reference", None)
        if isinstance(order_ref, str) and order_ref.startswith("in_"):
            return order_ref
    return None


def _subscription_ref_from_invoice(invoice: Any) -> Any:
    """Legacy `invoice.subscription` or new `invoice.parent.subscription_details`."""
    try:
        subscription = invoice["subscription"]
    except (KeyError, TypeError, AttributeError):
        subscription = getattr(invoice, "subscription", None)
    if subscription:
        return subscription

    try:
        parent = invoice["parent"]
    except (KeyError, TypeError, AttributeError):
        parent = getattr(invoice, "parent", None)
    if not parent:
        return None
    if isinstance(parent, dict):
        details = parent.get("subscription_details") or {}
        return details.get("subscription")
    details = getattr(parent, "subscription_details", None)
    if details is None:
        return None
    if isinstance(details, dict):
        return details.get("subscription")
    return getattr(details, "subscription", None)


def _subscription_metadata_from_invoice(invoice: Any) -> dict[str, str]:
    """Checkout metadata may live on invoice.parent.subscription_details.metadata."""
    try:
        parent = invoice["parent"]
    except (KeyError, TypeError, AttributeError):
        parent = getattr(invoice, "parent", None)
    if not parent:
        return {}
    if isinstance(parent, dict):
        details = parent.get("subscription_details") or {}
        raw = details.get("metadata") or {}
    else:
        details = getattr(parent, "subscription_details", None)
        if details is None:
            return {}
        raw = (
            details.get("metadata")
            if isinstance(details, dict)
            else getattr(details, "metadata", None)
        )
    if not raw:
        return {}
    if hasattr(raw, "to_dict"):
        raw = raw.to_dict()
    return {str(k): str(v) for k, v in dict(raw or {}).items() if v not in (None, "")}


def _attach_metadata_to_payment_intent(
    payment_intent_id: str | None,
    metadata: dict[str, str],
    *,
    stripe_account: str | None = None,
) -> None:
    """Copy checkout metadata onto the first invoice PI (webhook + /donations/record)."""
    if not payment_intent_id:
        return
    try:
        from stripe_intents import stripe_request_kwargs as _srk

        stripe.PaymentIntent.modify(
            payment_intent_id,
            metadata=metadata,
            **_srk(stripe_account),
        )
    except Exception:
        logging.getLogger(__name__).exception(
            "Failed to attach checkout metadata to PaymentIntent %s",
            payment_intent_id,
        )


def _cancel_incomplete_subscription(
    subscription_id: str | None,
    *,
    stripe_account: str | None = None,
) -> None:
    if not subscription_id:
        return
    try:
        from stripe_intents import stripe_request_kwargs as _srk

        stripe.Subscription.cancel(subscription_id, **_srk(stripe_account))
    except Exception:
        try:
            from stripe_intents import stripe_request_kwargs as _srk

            stripe.Subscription.delete(subscription_id, **_srk(stripe_account))
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to cancel incomplete subscription %s",
                subscription_id,
            )


def _create_monthly_subscription(
    *,
    customer_id: str,
    display_currency: str,
    payment_method: PaymentMethodType,
    base_amount: float,
    total_display: float,
    cover_fees: bool,
    metadata: dict[str, str],
    stripe_account: str | None = None,
) -> CheckoutResponse:
    """Create an incomplete monthly subscription and return checkout secrets."""
    charge_curr = charge_currency(display_currency, payment_method)
    charge_total = convert_for_charge(total_display, display_currency, payment_method)
    stripe_amount = to_stripe_amount(charge_total, charge_curr)

    full_metadata = {
        **metadata,
        "frequency": "monthly",
        "cover_fees": str(cover_fees).lower(),
        "base_amount": str(base_amount),
        "charge_currency": charge_curr.upper(),
        "charge_amount": str(charge_total),
        "total_display": str(total_display),
    }

    product_kwargs: dict = {"name": "Monthly Donation"}
    if stripe_account:
        product_kwargs["stripe_account"] = stripe_account
    product = stripe.Product.create(**product_kwargs)
    price_kwargs: dict = {
        "unit_amount": stripe_amount,
        "currency": charge_curr,
        "recurring": {"interval": "month"},
        "product": product.id,
    }
    if stripe_account:
        price_kwargs["stripe_account"] = stripe_account
    price = stripe.Price.create(**price_kwargs)
    sub_kwargs: dict = {
        "customer": customer_id,
        "items": [{"price": price.id}],
        "payment_behavior": "default_incomplete",
        "payment_settings": {
            "payment_method_types": _payment_method_types(charge_curr, payment_method),
            "save_default_payment_method": "on_subscription",
        },
        "expand": [
            # Stripe allows at most 4 expand levels — do not add payment_intent under payments.
            "latest_invoice.confirmation_secret",
            "latest_invoice.payments.data.payment",
        ],
        "metadata": full_metadata,
    }
    if stripe_account:
        sub_kwargs["stripe_account"] = stripe_account
    subscription = stripe.Subscription.create(**sub_kwargs)
    client_secret, payment_intent_id = _subscription_payment_details(
        subscription,
        stripe_account=stripe_account,
    )
    pi_metadata = {
        **full_metadata,
        "subscription_id": subscription.id,
        "stripe_customer_id": customer_id,
    }
    _attach_metadata_to_payment_intent(
        payment_intent_id,
        pi_metadata,
        stripe_account=stripe_account,
    )

    return _build_checkout_response(
        client_secret=client_secret,
        payment_intent_id=payment_intent_id,
        display_currency=display_currency,
        payment_method=payment_method,
        base_amount=base_amount,
        total_display=total_display,
        frequency="monthly",
        subscription_id=subscription.id,
        stripe_connect_account=stripe_account,
    )


def _subscription_payment_details(
    subscription: stripe.Subscription,
    *,
    stripe_account: str | None = None,
) -> tuple[str, str | None]:
    """Resolve client_secret for incomplete subscriptions across Stripe API versions."""
    retrieve_kwargs: dict = {}
    if stripe_account:
        retrieve_kwargs["stripe_account"] = stripe_account

    invoice = subscription.latest_invoice
    invoice_id = invoice if isinstance(invoice, str) else getattr(invoice, "id", None)
    needs_retrieve = isinstance(invoice, str) or not getattr(invoice, "confirmation_secret", None)
    if needs_retrieve and invoice_id:
        # Max 4 expand levels on Invoice (payments.data.payment.payment_intent).
        invoice = stripe.Invoice.retrieve(
            str(invoice_id),
            expand=["confirmation_secret", "payments.data.payment.payment_intent", "payment_intent"],
            **retrieve_kwargs,
        )

    if not invoice:
        raise HTTPException(status_code=500, detail="Unable to create subscription payment")

    confirmation = getattr(invoice, "confirmation_secret", None)
    if confirmation and getattr(confirmation, "client_secret", None):
        secret = confirmation.client_secret
        pi_id = _payment_intent_id_from_invoice(invoice) or _payment_intent_id_from_client_secret(
            secret
        )
        return secret, pi_id

    payment_intent = getattr(invoice, "payment_intent", None)
    if payment_intent:
        if isinstance(payment_intent, str):
            payment_intent = stripe.PaymentIntent.retrieve(payment_intent, **retrieve_kwargs)
        if payment_intent.client_secret:
            return payment_intent.client_secret, payment_intent.id

    # Newer invoice-payment objects may only expose the PI id on payments[].payment.
    pi_id = _payment_intent_id_from_invoice(invoice)
    if pi_id:
        payment_intent = stripe.PaymentIntent.retrieve(pi_id, **retrieve_kwargs)
        if payment_intent.client_secret:
            return payment_intent.client_secret, payment_intent.id

    raise HTTPException(status_code=500, detail="Unable to create subscription payment")


def _build_checkout_response(
    *,
    payment_intent: stripe.PaymentIntent | None = None,
    client_secret: str | None = None,
    payment_intent_id: str | None = None,
    display_currency: str,
    payment_method: PaymentMethodType,
    base_amount: float,
    total_display: float,
    frequency: Literal["once", "monthly"],
    subscription_id: str | None = None,
    stripe_connect_account: str | None = None,
) -> CheckoutResponse:
    charge_curr = charge_currency(display_currency, payment_method)
    charge_total = convert_for_charge(total_display, display_currency, payment_method)
    resolved_secret = client_secret or (payment_intent.client_secret if payment_intent else None)
    resolved_payment_intent_id = (
        payment_intent_id
        or (payment_intent.id if payment_intent else None)
        or _payment_intent_id_from_client_secret(resolved_secret)
    )

    if not resolved_secret:
        raise HTTPException(status_code=500, detail="Unable to create subscription payment")

    return CheckoutResponse(
        client_secret=resolved_secret,
        payment_intent_id=resolved_payment_intent_id,
        subscription_id=subscription_id,
        display_amount=format_display_amount(total_display, display_currency),
        base_amount=base_amount,
        total_amount=total_display,
        currency=display_currency.upper(),
        display_currency=display_currency.upper(),
        charge_currency=charge_curr.upper(),
        charge_amount=charge_total,
        conversion_note=conversion_note(display_currency, payment_method, total_display),
        frequency=frequency,
        paypal_available=paypal_available(display_currency),
        google_pay_available=True,
        stripe_connect_account=stripe_connect_account,
    )


def _intent_metadata(payment_intent: stripe.PaymentIntent) -> dict[str, str]:
    raw = payment_intent.metadata
    if not raw:
        return {}
    return raw.to_dict()


def _payload_from_intent(
    existing: stripe.PaymentIntent,
    payment_method: PaymentMethodType,
    cover_fees: bool,
    *,
    stripe_account: str | None = None,
) -> CreateCheckoutRequest:
    meta = _metadata_from_payment_intent(existing, stripe_account=stripe_account)
    display_currency = meta.get("display_currency", existing.currency.upper())
    base_amount = float(meta.get("base_amount", from_stripe_amount(existing.amount, existing.currency)))
    checkout_view = meta.get("checkout_view", "homepage")
    if checkout_view not in {"homepage", "popup", "landing"}:
        checkout_view = "homepage"

    return CreateCheckoutRequest(
        amount=base_amount,
        currency=display_currency,
        frequency=meta.get("frequency", "once"),  # type: ignore[arg-type]
        cover_fees=cover_fees,
        dedicate=meta.get("dedicate", "false").lower() == "true",
        honoree_name=meta.get("honoree_name") or None,
        comment=meta.get("comment") or None,
        payment_method=payment_method,
        campaign_id=meta.get("campaign_id"),
        checkout_view=checkout_view,  # type: ignore[arg-type]
        donor=DonorDetails(
            first_name=meta.get("first_name", ""),
            last_name=meta.get("last_name", ""),
            email=meta.get("email", ""),
            phone=meta.get("phone") or None,
        ),
    )


@app.post("/checkout/create", response_model=CheckoutResponse)
def create_checkout(payload: CreateCheckoutRequest) -> CheckoutResponse:
    from db import rest_get_one
    from routers.stripe_connect import resolve_stripe_account_for_checkout
    from site_constants import ROOT_CAMPAIGN_ID, ROOT_ORG_ID

    display_currency = payload.currency.lower()
    payment_method = payload.payment_method
    organization_id: str | None = None
    campaign_slug: str | None = None
    stripe_account: str | None = None

    is_root_checkout = not payload.campaign_id or payload.campaign_id == ROOT_CAMPAIGN_ID
    use_platform_payment_accounts = is_root_checkout or payload.checkout_view == "landing"

    # Homepage / pop-up / landing must use Super Admin → Payment accounts (Connect), not org settings.
    if use_platform_payment_accounts:
        from routers.payment_accounts import resolve_root_stripe_account

        stripe_account = resolve_root_stripe_account(payload.checkout_view)

    if payload.campaign_id and _is_uuid(payload.campaign_id):
        campaign = rest_get_one(
            "campaigns",
            params={
                "id": f"eq.{payload.campaign_id}",
                "select": "id,organization_id,slug,status,min_donation_amount,min_donation_amount_once,min_donation_amount_monthly,default_currency",
            },
        )
        if campaign:
            if campaign.get("status") != "live":
                raise HTTPException(status_code=400, detail="Campaign is not available for checkout")
            from currency import assert_meets_min_donation, resolve_min_donation_for_frequency

            assert_meets_min_donation(
                payload.amount,
                payload.currency,
                min_donation_amount=resolve_min_donation_for_frequency(campaign, payload.frequency),
                default_currency=campaign.get("default_currency"),
            )
            organization_id = campaign["organization_id"]
            campaign_slug = campaign["slug"]
            if not use_platform_payment_accounts:
                stripe_account, _ = resolve_stripe_account_for_checkout(
                    organization_id, payload.campaign_id
                )
        elif payload.campaign_id != ROOT_CAMPAIGN_ID:
            raise HTTPException(status_code=400, detail="Campaign is not available for checkout")

    checkout_campaign_id = payload.campaign_id
    if is_root_checkout:
        organization_id = ROOT_ORG_ID
        checkout_campaign_id = ROOT_CAMPAIGN_ID

    if not stripe_account and not use_platform_payment_accounts:
        raise HTTPException(
            status_code=400,
            detail=(
                "No Stripe account is available for this campaign. "
                "Connect Stripe in admin payment methods, or reconnect if the account "
                "was linked under a different Stripe platform."
            ),
        )

    base_amount, total_display = _resolve_amounts(payload.amount, display_currency, payload.cover_fees)
    metadata = _checkout_metadata(
        payload,
        base_amount,
        payment_method,
        organization_id=organization_id,
        campaign_id=checkout_campaign_id,
        campaign_slug=campaign_slug,
        stripe_account=stripe_account,
    )

    try:
        customer_kwargs: dict = {
            "metadata": metadata,
        }
        donor_email = _clean_donor_email(payload.donor.email)
        donor_name = " ".join(
            part
            for part in (
                _clean_donor_name(payload.donor.first_name),
                _clean_donor_name(payload.donor.last_name),
            )
            if part
        ).strip()
        if donor_email:
            customer_kwargs["email"] = donor_email
        if donor_name:
            customer_kwargs["name"] = donor_name
        if payload.donor.phone:
            customer_kwargs["phone"] = payload.donor.phone
        if stripe_account:
            customer_kwargs["stripe_account"] = stripe_account
        customer = stripe.Customer.create(**customer_kwargs)

        if payload.frequency == "monthly":
            return _create_monthly_subscription(
                customer_id=customer.id,
                display_currency=display_currency,
                payment_method=payment_method,
                base_amount=base_amount,
                total_display=total_display,
                cover_fees=payload.cover_fees,
                metadata=metadata,
                stripe_account=stripe_account,
            )

        payment_intent = _create_once_payment_intent(
            display_currency=display_currency,
            payment_method=payment_method,
            base_amount=base_amount,
            total_display=total_display,
            cover_fees=payload.cover_fees,
            customer_id=customer.id,
            metadata=metadata,
            stripe_account=stripe_account,
        )

        return _build_checkout_response(
            payment_intent=payment_intent,
            display_currency=display_currency,
            payment_method=payment_method,
            base_amount=base_amount,
            total_display=total_display,
            frequency="once",
            stripe_connect_account=stripe_account,
        )
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=400, detail=str(exc.user_message or exc)) from exc


def _customer_id(customer: str | stripe.Customer | None) -> str:
    if customer is None:
        raise HTTPException(status_code=400, detail="Payment intent has no customer.")
    if isinstance(customer, str):
        return customer
    return customer.id


@app.post("/checkout/{payment_intent_id}/switch-method", response_model=CheckoutResponse)
def switch_payment_method(payment_intent_id: str, payload: SwitchPaymentMethodRequest) -> CheckoutResponse:
    from stripe_intents import retrieve_payment_intent, stripe_request_kwargs

    try:
        existing, stripe_account = retrieve_payment_intent(payment_intent_id)
        stripe_kwargs = stripe_request_kwargs(stripe_account)
        if existing.status in {"canceled", "succeeded"}:
            raise HTTPException(status_code=400, detail="This payment session is no longer active.")

        meta = _metadata_from_payment_intent(existing, stripe_account=stripe_account)
        display_currency = meta.get("display_currency", existing.currency.upper()).lower()
        payment_method = payload.payment_method

        if payment_method == "paypal" and not supports_paypal(display_currency):
            raise HTTPException(status_code=400, detail="PayPal is only available for USD donations.")

        checkout_payload = _payload_from_intent(
            existing,
            payment_method,
            payload.cover_fees,
            stripe_account=stripe_account,
        )
        base_amount, total_display = _resolve_amounts(
            checkout_payload.amount,
            display_currency,
            payload.cover_fees,
        )

        frequency = meta.get("frequency", "once")
        subscription_id = meta.get("subscription_id")
        if frequency == "monthly" or subscription_id:
            customer_id = _customer_id(getattr(existing, "customer", None) or meta.get("stripe_customer_id"))
            _cancel_incomplete_subscription(subscription_id, stripe_account=stripe_account)
            switch_meta = {
                k: str(v)
                for k, v in meta.items()
                if v is not None and k not in {"subscription_id"}
            }
            switch_meta["payment_method"] = payment_method
            switch_meta["cover_fees"] = str(payload.cover_fees).lower()
            return _create_monthly_subscription(
                customer_id=customer_id,
                display_currency=display_currency,
                payment_method=payment_method,
                base_amount=base_amount,
                total_display=total_display,
                cover_fees=payload.cover_fees,
                metadata=switch_meta,
                stripe_account=stripe_account,
            )

        charge_curr = charge_currency(display_currency, payment_method)
        charge_total = convert_for_charge(total_display, display_currency, payment_method)
        stripe_amount = to_stripe_amount(charge_total, charge_curr)
        metadata = _checkout_metadata(checkout_payload, base_amount, payment_method)

        payment_intent = stripe.PaymentIntent.modify(
            payment_intent_id,
            amount=stripe_amount,
            currency=charge_curr,
            payment_method_types=_payment_method_types(charge_curr, payment_method),
            metadata={
                **meta,
                **metadata,
                "charge_currency": charge_curr.upper(),
                "charge_amount": str(charge_total),
                "total_display": str(total_display),
                "cover_fees": str(payload.cover_fees).lower(),
            },
            **stripe_kwargs,
        )

        return _build_checkout_response(
            payment_intent=payment_intent,
            display_currency=display_currency,
            payment_method=payment_method,
            base_amount=base_amount,
            total_display=total_display,
            frequency=frequency if frequency in {"once", "monthly"} else "once",
            stripe_connect_account=stripe_account or meta.get("stripe_connect_account"),
        )
    except HTTPException:
        raise
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=400, detail=str(exc.user_message or exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.patch("/checkout/{payment_intent_id}", response_model=CheckoutResponse)
def update_checkout(payment_intent_id: str, payload: UpdateCheckoutRequest) -> CheckoutResponse:
    from stripe_intents import retrieve_payment_intent, stripe_request_kwargs

    try:
        existing, stripe_account = retrieve_payment_intent(payment_intent_id)
        stripe_kwargs = stripe_request_kwargs(stripe_account)
        meta = _metadata_from_payment_intent(existing, stripe_account=stripe_account)
        display_currency = meta.get("display_currency", existing.currency.upper()).lower()
        payment_method: PaymentMethodType = meta.get("payment_method", "card")  # type: ignore[assignment]
        base_amount = float(meta.get("base_amount", from_stripe_amount(existing.amount, existing.currency)))
        _, total_display = _resolve_amounts(base_amount, display_currency, payload.cover_fees)
        frequency = meta.get("frequency", "once")
        subscription_id = meta.get("subscription_id")

        # Invoice-backed subscription PaymentIntents cannot have their amount modified.
        # Cancel the incomplete subscription and create a new one with the updated total.
        if frequency == "monthly" or subscription_id:
            customer_id = _customer_id(getattr(existing, "customer", None) or meta.get("stripe_customer_id"))
            if not customer_id:
                raise HTTPException(status_code=400, detail="Payment intent has no customer.")
            _cancel_incomplete_subscription(subscription_id, stripe_account=stripe_account)
            refreshed_meta = {
                k: str(v)
                for k, v in meta.items()
                if v is not None and k not in {"subscription_id"}
            }
            return _create_monthly_subscription(
                customer_id=customer_id,
                display_currency=display_currency,
                payment_method=payment_method,
                base_amount=base_amount,
                total_display=total_display,
                cover_fees=payload.cover_fees,
                metadata=refreshed_meta,
                stripe_account=stripe_account,
            )

        charge_total = convert_for_charge(total_display, display_currency, payment_method)
        charge_curr = charge_currency(display_currency, payment_method)
        stripe_amount = to_stripe_amount(charge_total, charge_curr)

        updated = stripe.PaymentIntent.modify(
            payment_intent_id,
            amount=stripe_amount,
            metadata={
                **meta,
                "cover_fees": str(payload.cover_fees).lower(),
                "charge_amount": str(charge_total),
                "total_display": str(total_display),
            },
            **stripe_kwargs,
        )

        return _build_checkout_response(
            payment_intent=updated,
            display_currency=display_currency,
            payment_method=payment_method,
            base_amount=base_amount,
            total_display=total_display,
            frequency=frequency if frequency in {"once", "monthly"} else "once",
            stripe_connect_account=stripe_account or meta.get("stripe_connect_account"),
        )
    except HTTPException:
        raise
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=400, detail=str(exc.user_message or exc)) from exc


def _metadata_from_payment_intent(
    payment_intent: stripe.PaymentIntent,
    *,
    stripe_account: str | None = None,
) -> dict[str, str]:
    from stripe_intents import stripe_request_kwargs

    meta = _intent_metadata(payment_intent)
    sub_meta: dict[str, str] = {}
    acct_kwargs = stripe_request_kwargs(stripe_account)

    subscription = None
    subscription_id = meta.get("subscription_id")
    invoice_id = _invoice_id_from_payment_intent(payment_intent)

    try:
        if invoice_id:
            invoice = stripe.Invoice.retrieve(
                str(invoice_id),
                expand=["subscription", "parent"],
                **acct_kwargs,
            )
            # New Stripe invoices often carry checkout metadata on parent.subscription_details.
            sub_meta = {**_subscription_metadata_from_invoice(invoice), **sub_meta}
            subscription = _subscription_ref_from_invoice(invoice)
        elif subscription_id:
            subscription = stripe.Subscription.retrieve(subscription_id, **acct_kwargs)

        if isinstance(subscription, str) and subscription:
            subscription = stripe.Subscription.retrieve(subscription, **acct_kwargs)
        if subscription is not None and not isinstance(subscription, str):
            raw = getattr(subscription, "metadata", None)
            if raw:
                from_sub = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
                sub_meta = {**from_sub, **sub_meta}
            sid = getattr(subscription, "id", None) or (
                subscription.get("id") if isinstance(subscription, dict) else None
            )
            if sid and not meta.get("subscription_id") and not sub_meta.get("subscription_id"):
                sub_meta = {**sub_meta, "subscription_id": str(sid)}
        elif isinstance(subscription, str) and subscription and not sub_meta.get("subscription_id"):
            sub_meta = {**sub_meta, "subscription_id": subscription}
        elif subscription_id and not sub_meta.get("subscription_id"):
            sub_meta = {**sub_meta, "subscription_id": subscription_id}
    except Exception:
        logging.getLogger(__name__).exception(
            "Failed to merge subscription metadata for PaymentIntent %s",
            getattr(payment_intent, "id", None),
        )
        return {**sub_meta, **meta}

    # Subscription holds the checkout fields; non-empty PI keys win when present.
    return {**sub_meta, **{k: v for k, v in meta.items() if v not in (None, "")}}


def _split_payer_name(full_name: str | None) -> tuple[str, str]:
    cleaned = " ".join((full_name or "").split()).strip()
    if not cleaned:
        return "", ""
    parts = cleaned.split(" ")
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _is_placeholder_donor(first_name: str | None, last_name: str | None, email: str | None) -> bool:
    first = (first_name or "").strip().lower()
    last = (last_name or "").strip().lower()
    mail = (email or "").strip().lower()
    placeholder_first = first in {"", "donor", "anonymous", "guest"}
    placeholder_last = last in {"", "guest", "donor", "anonymous"}
    placeholder_email = mail in {"", "pending@wallet.local", "donor@example.com"}
    return (placeholder_first and placeholder_last) or placeholder_email


def _clean_donor_name(value: str | None) -> str:
    cleaned = (value or "").strip()
    if cleaned.lower() in {"", "donor", "anonymous", "guest"}:
        return ""
    return cleaned


def _clean_donor_email(value: str | None) -> str:
    cleaned = (value or "").strip()
    if cleaned.lower() in {"", "pending@wallet.local", "donor@example.com"}:
        return ""
    return cleaned


def _normalize_donation_donor(
    first_name: str | None,
    last_name: str | None,
    email: str | None,
) -> tuple[str, str, str | None]:
    """Never persist Guest / Donor / pending@wallet.local placeholders."""
    first = _clean_donor_name(first_name)
    last = _clean_donor_name(last_name)
    mail = _clean_donor_email(email) or None
    if not first and not last:
        first = "Anonymous"
        last = ""
    return first, last, mail


def _billing_details_from_intent(
    payment_intent: stripe.PaymentIntent,
    *,
    stripe_account: str | None = None,
) -> tuple[str, str, str | None]:
    """Return (first_name, last_name, email) from the charged payment method when present."""
    from stripe_intents import stripe_request_kwargs

    billing_name = ""
    billing_email: str | None = None

    def read_billing(obj: object | None) -> None:
        nonlocal billing_name, billing_email
        if obj is None:
            return
        try:
            details = obj["billing_details"]  # type: ignore[index]
        except Exception:
            details = getattr(obj, "billing_details", None)
        if not details:
            return
        try:
            name = details["name"]
        except Exception:
            name = getattr(details, "name", None)
        try:
            email = details["email"]
        except Exception:
            email = getattr(details, "email", None)
        if isinstance(name, str) and name.strip() and not billing_name:
            billing_name = name.strip()
        if isinstance(email, str) and email.strip() and not billing_email:
            billing_email = email.strip()

    payment_method = None
    try:
        payment_method = payment_intent["payment_method"]
    except Exception:
        payment_method = getattr(payment_intent, "payment_method", None)

    if isinstance(payment_method, str) and payment_method.strip():
        try:
            payment_method = stripe.PaymentMethod.retrieve(
                payment_method.strip(),
                **stripe_request_kwargs(stripe_account),
            )
        except Exception:
            payment_method = None

    read_billing(payment_method)

    if not billing_name or not billing_email:
        try:
            latest_charge = payment_intent["latest_charge"]
        except Exception:
            latest_charge = getattr(payment_intent, "latest_charge", None)
        if isinstance(latest_charge, str) and latest_charge.strip():
            try:
                latest_charge = stripe.Charge.retrieve(
                    latest_charge.strip(),
                    **stripe_request_kwargs(stripe_account),
                )
            except Exception:
                latest_charge = None
        read_billing(latest_charge)

    first_name, last_name = _split_payer_name(billing_name)
    return first_name, last_name, billing_email


def _donation_row_from_intent(
    payment_intent: stripe.PaymentIntent,
    *,
    stripe_account: str | None = None,
) -> dict[str, str | float | None | dict[str, str]]:
    meta = _metadata_from_payment_intent(payment_intent, stripe_account=stripe_account)
    display_currency = meta.get("display_currency", payment_intent.currency.upper()).upper()
    total_display = float(meta.get("total_display", from_stripe_amount(payment_intent.amount, payment_intent.currency)))

    base_amount = float(meta.get("base_amount", total_display))
    cover_fees = meta.get("cover_fees", "false").lower() == "true"
    if cover_fees:
        processing_fee = max(0.0, round(total_display - base_amount, 2))
        payout_amount = base_amount
    else:
        processing_fee = estimate_processing_fee(base_amount, display_currency)
        payout_amount = max(0.0, round(base_amount - processing_fee, 2))

    customer_id = None
    try:
        customer_ref = payment_intent["customer"]
    except (KeyError, TypeError, AttributeError):
        customer_ref = None
    if customer_ref:
        customer_id = customer_ref if isinstance(customer_ref, str) else getattr(customer_ref, "id", None)

    subscription_id = meta.get("subscription_id") or None
    invoice_id = _invoice_id_from_payment_intent(payment_intent)
    if invoice_id and not subscription_id:
        try:
            from stripe_intents import stripe_request_kwargs

            invoice = stripe.Invoice.retrieve(
                str(invoice_id),
                expand=["subscription", "parent"],
                **stripe_request_kwargs(stripe_account),
            )
            sub = _subscription_ref_from_invoice(invoice)
            if isinstance(sub, str):
                subscription_id = sub
            elif sub is not None:
                subscription_id = getattr(sub, "id", None)
        except Exception:
            subscription_id = subscription_id or None

    frequency = meta.get("frequency", "once")
    # Subscription / invoice-backed PaymentIntents must count as monthly (admin Recurring tab).
    if subscription_id or invoice_id:
        frequency = "monthly"
    elif frequency not in {"once", "monthly"}:
        frequency = "once"

    first_name = meta.get("first_name", "") or ""
    last_name = meta.get("last_name", "") or ""
    email = meta.get("email") or None
    if _is_placeholder_donor(first_name, last_name, email):
        billing_first, billing_last, billing_email = _billing_details_from_intent(
            payment_intent,
            stripe_account=stripe_account,
        )
        if billing_first:
            first_name = billing_first
            last_name = billing_last
        if billing_email:
            email = billing_email

    first_name, last_name, email = _normalize_donation_donor(first_name, last_name, email)

    row: dict[str, str | float | None | dict[str, str] | bool] = {
        "stripe_payment_intent_id": payment_intent.id,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "amount": total_display,
        "base_amount": base_amount,
        "currency": display_currency,
        "frequency": frequency,
        "payment_method": meta.get("payment_method"),
        "payment_processor": "stripe",
        "honoree_name": meta.get("honoree_name") or None,
        "comment": meta.get("comment") or None,
        "organization_id": meta.get("organization_id"),
        "campaign_id": meta.get("campaign_id"),
        "status": "succeeded",
        "fee_covered": cover_fees,
        "platform_fee": 0,
        "processing_fee": processing_fee,
        "payout_amount": payout_amount,
        "stripe_customer_id": customer_id,
        "stripe_subscription_id": subscription_id,
    }
    if stripe_account:
        row["stripe_account_id"] = stripe_account
    utm = _utm_from_meta(meta)
    if utm:
        row["utm"] = utm
    row["device"] = _device_from_meta(meta)
    return row


from routers.super_admin import router as super_admin_router
from routers.platform_data import router as platform_data_router
from routers.organizations import router as organizations_router
from routers.public import router as public_router
from routers.stripe_connect import router as stripe_router
from routers.invites import router as invites_router
from routers.admin_data import router as admin_data_router
from routers.paypal import router as paypal_router
from routers.paypal_connect import router as paypal_connect_router
from routers.authorizenet import router as authorizenet_router
from routers.nowpayments import router as nowpayments_router
from routers.payment_accounts import router as payment_accounts_router
from routers.emails import router as emails_router
from routers.uploads import router as uploads_router
from routers.donor_portal import router as donor_portal_router
from routers.ai_content import router as ai_content_router
from routers.testing_paypal import router as testing_paypal_router
from routers.conversion_events import router as conversion_events_router

app.include_router(super_admin_router)
app.include_router(platform_data_router)
app.include_router(payment_accounts_router)
app.include_router(organizations_router)
app.include_router(public_router)
app.include_router(stripe_router)
app.include_router(invites_router)
app.include_router(admin_data_router)
app.include_router(ai_content_router)
app.include_router(paypal_router)
app.include_router(paypal_connect_router)
app.include_router(authorizenet_router)
app.include_router(nowpayments_router)
app.include_router(emails_router)
app.include_router(uploads_router)
app.include_router(donor_portal_router)
app.include_router(testing_paypal_router)
app.include_router(conversion_events_router)


@app.on_event("startup")
def bootstrap_platform_domains() -> None:
    try:
        from storage_upload import ensure_campaign_assets_bucket

        ensure_campaign_assets_bucket()
    except Exception:
        pass
    try:
        from routers.organizations import ensure_root_campaign_subdomain

        ensure_root_campaign_subdomain()
    except Exception:
        pass


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> dict[str, str]:
    import json

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    try:
        if secret:
            event = stripe.Webhook.construct_event(payload, sig, secret)
        else:
            event = stripe.Event.construct_from(json.loads(payload), stripe.api_key)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if event["type"] == "payment_intent.succeeded":
        pi = event["data"]["object"]
        meta = dict(pi.get("metadata", {}) or {})
        stripe_account = (
            meta.get("stripe_connect_account")
            or pi.get("on_behalf_of")
            or (pi.get("transfer_data") or {}).get("destination")
        )
        # Monthly invoice PIs often lack checkout metadata; merge from the subscription.
        invoice_id = pi.get("invoice")
        if not invoice_id:
            details = pi.get("payment_details") if isinstance(pi.get("payment_details"), dict) else {}
            order_ref = details.get("order_reference")
            if isinstance(order_ref, str) and order_ref.startswith("in_"):
                invoice_id = order_ref
        subscription_id = meta.get("subscription_id")
        try:
            from stripe_intents import stripe_request_kwargs as _srk

            acct_kwargs = _srk(stripe_account if isinstance(stripe_account, str) else None)
            # Connect subscription PIs often omit account hints on the event object.
            if not acct_kwargs.get("stripe_account"):
                try:
                    from stripe_intents import retrieve_payment_intent as _retrieve_pi

                    _, resolved_acct = _retrieve_pi(pi["id"])
                    if resolved_acct:
                        stripe_account = resolved_acct
                        acct_kwargs = _srk(resolved_acct)
                except Exception:
                    pass
            subscription = None
            if invoice_id:
                invoice = stripe.Invoice.retrieve(
                    invoice_id if isinstance(invoice_id, str) else invoice_id.get("id"),
                    expand=["subscription", "parent"],
                    **acct_kwargs,
                )
                parent_meta = _subscription_metadata_from_invoice(invoice)
                if parent_meta:
                    meta = {**parent_meta, **{k: v for k, v in meta.items() if v not in (None, "")}}
                subscription = _subscription_ref_from_invoice(invoice)
            elif subscription_id:
                subscription = stripe.Subscription.retrieve(subscription_id, **acct_kwargs)
            if isinstance(subscription, str) and subscription:
                subscription = stripe.Subscription.retrieve(subscription, **acct_kwargs)
            if subscription is not None and not isinstance(subscription, str):
                raw = getattr(subscription, "metadata", None)
                if raw is None and isinstance(subscription, dict):
                    raw = subscription.get("metadata")
                sub_meta = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw or {})
                meta = {**sub_meta, **{k: v for k, v in meta.items() if v not in (None, "")}}
                if not meta.get("subscription_id"):
                    sid = getattr(subscription, "id", None) or (
                        subscription.get("id") if isinstance(subscription, dict) else None
                    )
                    if sid:
                        meta["subscription_id"] = sid
                if not stripe_account:
                    stripe_account = meta.get("stripe_connect_account")
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to merge subscription metadata for PaymentIntent %s",
                pi.get("id"),
            )

        display_currency = meta.get("display_currency", pi["currency"]).upper()
        total_display = float(meta.get("total_display", pi["amount"] / 100))
        base_amount = float(meta.get("base_amount", total_display))
        cover_fees = meta.get("cover_fees", "false").lower() == "true"
        if cover_fees:
            processing_fee = max(0.0, round(total_display - base_amount, 2))
            payout_amount = base_amount
        else:
            processing_fee = estimate_processing_fee(base_amount, display_currency)
            payout_amount = max(0.0, round(base_amount - processing_fee, 2))

        first_name, last_name, email = _normalize_donation_donor(
            meta.get("first_name"),
            meta.get("last_name"),
            meta.get("email"),
        )
        # Prefer PaymentMethod billing when checkout still had empty wallet donor fields.
        if _is_placeholder_donor(first_name, last_name, email) or first_name == "Anonymous":
            try:
                from stripe_intents import retrieve_payment_intent

                pi_obj, acct = retrieve_payment_intent(
                    pi["id"],
                    stripe_account=stripe_account if isinstance(stripe_account, str) else None,
                    expand=["payment_method", "latest_charge"],
                )
                billing_first, billing_last, billing_email = _billing_details_from_intent(
                    pi_obj,
                    stripe_account=acct,
                )
                if billing_first or billing_email:
                    first_name, last_name, email = _normalize_donation_donor(
                        billing_first or first_name,
                        billing_last if billing_first else last_name,
                        billing_email or email,
                    )
            except Exception:
                pass

        frequency = meta.get("frequency", "once")
        subscription_id = meta.get("subscription_id")
        # Invoice-backed subscription charges are monthly even when PI metadata is thin.
        if subscription_id or invoice_id:
            frequency = "monthly"
        elif frequency not in {"once", "monthly"}:
            frequency = "once"

        row = {
            "stripe_payment_intent_id": pi["id"],
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "amount": total_display,
            "base_amount": base_amount,
            "currency": display_currency,
            "frequency": frequency,
            "payment_method": meta.get("payment_method"),
            "payment_processor": "stripe",
            "honoree_name": meta.get("honoree_name"),
            "comment": meta.get("comment"),
            "status": "succeeded",
            "organization_id": meta.get("organization_id"),
            "campaign_id": meta.get("campaign_id"),
            "stripe_account_id": pi.get("on_behalf_of") or (pi.get("transfer_data") or {}).get("destination") or stripe_account,
            "stripe_customer_id": pi.get("customer") if isinstance(pi.get("customer"), str) else None,
            "stripe_subscription_id": subscription_id,
            "fee_covered": cover_fees,
            "platform_fee": 0,
            "processing_fee": processing_fee,
            "payout_amount": payout_amount,
        }
        utm = _utm_from_meta(meta)
        if utm:
            row["utm"] = utm
        row["device"] = _device_from_meta(meta)
        existing = get_donation_by_payment_intent(pi["id"])
        if existing and str(existing.get("status") or "").lower() == "failed":
            from db import rest_patch

            rest_patch(
                "donations",
                {
                    "status": "succeeded",
                    "frequency": frequency,
                    "processing_fee": processing_fee,
                    "payout_amount": payout_amount,
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": email,
                    "stripe_subscription_id": subscription_id,
                    "organization_id": row.get("organization_id"),
                    "campaign_id": row.get("campaign_id"),
                    "payment_processor": "stripe",
                },
                match={"id": existing["id"]},
            )
            saved = get_donation_by_payment_intent(pi["id"])
        else:
            saved = insert_donation(_ensure_donation_org({k: v for k, v in row.items() if v is not None}))
            if not saved:
                # Duplicate PI (e.g. race): upgrade existing row if it is not succeeded monthly yet.
                existing = get_donation_by_payment_intent(pi["id"])
                if existing:
                    from db import rest_patch

                    rest_patch(
                        "donations",
                        {
                            k: v
                            for k, v in _ensure_donation_org(row).items()
                            if v is not None and k != "stripe_payment_intent_id"
                        },
                        match={"id": existing["id"]},
                    )
                    saved = get_donation_by_payment_intent(pi["id"])
        if saved:
            _send_donation_emails_safe(saved)

    if event["type"] == "payment_intent.payment_failed":
        pi = event["data"]["object"]
        meta = pi.get("metadata", {}) or {}
        display_currency = str(meta.get("display_currency") or pi.get("currency") or "usd").upper()
        try:
            total_display = float(meta.get("total_display", (pi.get("amount") or 0) / 100))
        except (TypeError, ValueError):
            total_display = float((pi.get("amount") or 0) / 100)
        try:
            base_amount = float(meta.get("base_amount", total_display))
        except (TypeError, ValueError):
            base_amount = total_display
        last_error = pi.get("last_payment_error") if isinstance(pi.get("last_payment_error"), dict) else {}
        fail_msg = str(
            last_error.get("message")
            or last_error.get("code")
            or meta.get("failure_message")
            or "Card payment failed"
        )[:400]
        existing = get_donation_by_payment_intent(pi["id"])
        if existing:
            from db import rest_patch

            if str(existing.get("status") or "").lower() != "succeeded":
                comment = str(existing.get("comment") or "").strip()
                updates = {
                    "status": "failed",
                    "payout_amount": 0,
                    "comment": f"{comment + ' · ' if comment else ''}Payment failed: {fail_msg}"[:500],
                }
                rest_patch("donations", updates, match={"id": existing["id"]})
        else:
            row = {
                "stripe_payment_intent_id": pi["id"],
                "first_name": meta.get("first_name", "Anonymous"),
                "last_name": meta.get("last_name", ""),
                "email": meta.get("email"),
                "amount": total_display,
                "base_amount": base_amount,
                "currency": display_currency,
                "frequency": meta.get("frequency", "once"),
                "payment_method": meta.get("payment_method") or "card",
                "payment_processor": "stripe",
                "honoree_name": meta.get("honoree_name"),
                "comment": f"Payment failed: {fail_msg}",
                "status": "failed",
                "organization_id": meta.get("organization_id"),
                "campaign_id": meta.get("campaign_id"),
                "stripe_account_id": pi.get("on_behalf_of")
                or (pi.get("transfer_data") or {}).get("destination"),
                "stripe_customer_id": pi.get("customer") if isinstance(pi.get("customer"), str) else None,
                "stripe_subscription_id": meta.get("subscription_id"),
                "fee_covered": str(meta.get("cover_fees", "false")).lower() == "true",
                "platform_fee": 0,
                "processing_fee": 0,
                "payout_amount": 0,
            }
            utm = _utm_from_meta(meta)
            if utm:
                row["utm"] = utm
            row["device"] = _device_from_meta(meta)
            insert_donation(_ensure_donation_org({k: v for k, v in row.items() if v is not None}))

    if event["type"] == "account.updated":
        from db import rest_patch

        acct = event["data"]["object"]
        rest_patch(
            "stripe_accounts",
            {
                "connection_status": "active" if acct.get("charges_enabled") else "pending",
                "charges_enabled": bool(acct.get("charges_enabled")),
                "payouts_enabled": bool(acct.get("payouts_enabled")),
            },
            match={"stripe_account_id": acct["id"]},
        )

    return {"status": "ok"}


@app.get("/donations", response_model=DonationFeedResponse)
def get_donations(
    limit: int = 20,
    offset: int = 0,
    campaign_id: str | None = None,
    sort: str = "recent",
) -> DonationFeedResponse:
    if limit < 1 or limit > 50:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 50")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    if sort not in {"recent", "descending"}:
        raise HTTPException(status_code=400, detail="sort must be recent or descending")

    if not supabase_enabled():
        return DonationFeedResponse(donations=[], has_more=False)

    from currency import convert_to_reporting
    from db import rest_get as db_rest_get
    from db import rest_get_one as db_rest_get_one
    from site_constants import ROOT_CAMPAIGN_ID, ROOT_ORG_ID

    select_cols = (
        "id,first_name,last_name,amount,currency,frequency,honoree_name,created_at,device,"
        "crypto_amount,crypto_currency"
    )
    select_cols_no_device = (
        "id,first_name,last_name,amount,currency,frequency,honoree_name,created_at,"
        "crypto_amount,crypto_currency"
    )
    select_cols_basic = (
        "id,first_name,last_name,amount,currency,frequency,honoree_name,created_at"
    )
    amount_sort = sort == "descending"
    # Org-wide feed merges multiple campaigns, so page in memory after fetch.
    fetch_limit = max(200, offset + limit + 1)

    def _resolve_org_id() -> str | None:
        if not campaign_id or not _is_uuid(campaign_id):
            return None
        if campaign_id == ROOT_CAMPAIGN_ID:
            return ROOT_ORG_ID
        campaign = db_rest_get_one(
            "campaigns",
            params={"id": f"eq.{campaign_id}", "select": "organization_id"},
        )
        org_id = str((campaign or {}).get("organization_id") or "").strip()
        return org_id or None

    def _org_campaign_ids(org_id: str) -> list[str]:
        rows = db_rest_get(
            "campaigns",
            params={
                "organization_id": f"eq.{org_id}",
                "select": "id",
                "limit": "300",
            },
        ) or []
        ids = [str(c["id"]) for c in rows if c.get("id")]
        if org_id == ROOT_ORG_ID and ROOT_CAMPAIGN_ID not in ids:
            ids.append(ROOT_CAMPAIGN_ID)
        return ids

    def _fetch_org_wide(select: str, org_id: str) -> list:
        params = {
            "organization_id": f"eq.{org_id}",
            "select": select,
            "order": "created_at.desc",
            "limit": str(fetch_limit),
            "offset": "0",
        }
        rows = db_rest_get("donations", params=params) or []
        campaign_ids = _org_campaign_ids(org_id)
        if not campaign_ids:
            return rows
        orphan_params = {
            "organization_id": "is.null",
            "campaign_id": f"in.({','.join(campaign_ids)})",
            "select": select,
            "order": "created_at.desc",
            "limit": str(fetch_limit),
            "offset": "0",
        }
        orphans = db_rest_get("donations", params=orphan_params) or []
        if not orphans:
            return rows
        seen = {str(r.get("id")) for r in rows if r.get("id")}
        for row in orphans:
            row_id = str(row.get("id") or "")
            if row_id and row_id not in seen:
                rows.append(row)
                seen.add(row_id)
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return rows

    def _fetch_all_organizations(select: str) -> list:
        params = {
            "select": select,
            "order": "created_at.desc",
            "limit": str(fetch_limit),
            "offset": "0",
        }
        return db_rest_get("donations", params=params) or []

    def _recent_donations_all_organizations(cid: str | None) -> bool:
        """Yes (default): all orgs. No: this organization only."""
        if not cid or not _is_uuid(cid):
            return True
        try:
            content = db_rest_get_one(
                "campaign_content",
                params={
                    "campaign_id": f"eq.{cid}",
                    "select": "recent_donations_all_organizations",
                },
            )
        except Exception:
            return True
        if not content or content.get("recent_donations_all_organizations") is None:
            return True
        return bool(content.get("recent_donations_all_organizations"))

    def _fetch(select: str) -> list:
        org_id = _resolve_org_id()
        if campaign_id and _is_uuid(campaign_id) and _recent_donations_all_organizations(campaign_id):
            return _fetch_all_organizations(select)
        if org_id:
            return _fetch_org_wide(select, org_id)
        if campaign_id and _is_uuid(campaign_id):
            # Campaign without org still falls back to that campaign only.
            params = {
                "campaign_id": f"eq.{campaign_id}",
                "select": select,
                "order": "created_at.desc",
                "limit": str(fetch_limit),
                "offset": "0",
            }
            return db_rest_get("donations", params=params) or []
        return list_donations(limit=fetch_limit, offset=0)

    rows = _fetch(select_cols)
    if not rows:
        rows = _fetch(select_cols_no_device)
    if not rows:
        rows = _fetch(select_cols_basic)

    reporting_currency = "USD"
    org_id = _resolve_org_id()
    if org_id or (campaign_id and _is_uuid(campaign_id)):
        if org_id:
            org = db_rest_get_one(
                "organizations",
                params={"id": f"eq.{org_id}", "select": "reporting_currency,default_currency"},
            )
            reporting_currency = str(
                (org or {}).get("reporting_currency")
                or (org or {}).get("default_currency")
                or "USD"
            ).upper()
        else:
            campaign = db_rest_get_one(
                "campaigns",
                params={"id": f"eq.{campaign_id}", "select": "default_currency,organization_id"},
            )
            if campaign:
                org = db_rest_get_one(
                    "organizations",
                    params={
                        "id": f"eq.{campaign.get('organization_id')}",
                        "select": "reporting_currency,default_currency",
                    },
                )
                reporting_currency = str(
                    (org or {}).get("reporting_currency")
                    or campaign.get("default_currency")
                    or (org or {}).get("default_currency")
                    or "USD"
                ).upper()

    if amount_sort:
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        rows.sort(
            key=lambda r: convert_to_reporting(
                float(r.get("amount") or 0),
                str(r.get("currency") or "USD"),
                reporting_currency,
            ),
            reverse=True,
        )
    else:
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)

    page = rows[offset : offset + limit]
    has_more = len(rows) > offset + limit

    donations = [_feed_item_from_row(row) for row in page]
    return DonationFeedResponse(donations=donations, has_more=has_more)



def _ensure_donation_org(row: dict[str, Any]) -> dict[str, Any]:
    """Backfill organization_id from campaign so the gift appears in the correct admin list."""
    from site_constants import ROOT_CAMPAIGN_ID, ROOT_ORG_ID

    if row.get("organization_id"):
        return row
    campaign_id = row.get("campaign_id")
    if not campaign_id or campaign_id == ROOT_CAMPAIGN_ID:
        return {
            **row,
            "organization_id": ROOT_ORG_ID,
            "campaign_id": campaign_id or ROOT_CAMPAIGN_ID,
        }
    campaign = rest_get_one(
        "campaigns",
        params={"id": f"eq.{campaign_id}", "select": "organization_id"},
    )
    org_id = (campaign or {}).get("organization_id")
    if org_id:
        row = {**row, "organization_id": str(org_id)}
    return row


def _send_donation_emails_safe(saved: dict[str, Any]) -> None:
    """Email failures must never mark a successful payment as unrecorded."""
    if str(saved.get("status") or "").lower() != "succeeded":
        return
    try:
        from emails import send_donation_alerts_for_row, send_donation_confirmation_for_row

        send_donation_confirmation_for_row(saved)
        send_donation_alerts_for_row(saved)
    except Exception:
        logging.getLogger(__name__).exception(
            "Post-donation emails failed for donation %s",
            saved.get("id"),
        )


@app.post("/donations/record", response_model=DonationFeedItem)
def record_donation(payload: RecordDonationRequest) -> DonationFeedItem:
    from stripe_intents import retrieve_payment_intent

    if not supabase_enabled():
        raise HTTPException(status_code=503, detail="Donation storage is not configured")

    try:
        payment_intent, stripe_account = retrieve_payment_intent(
            payload.payment_intent_id,
            expand=["payment_method", "latest_charge"],
        )
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=400, detail=str(exc.user_message or exc)) from exc

    if payment_intent.status not in {"succeeded", "processing"}:
        raise HTTPException(status_code=400, detail="Payment has not succeeded yet")

    row = _ensure_donation_org(_donation_row_from_intent(payment_intent, stripe_account=stripe_account))
    if payment_intent.status == "processing":
        row["status"] = "succeeded"
    saved = insert_donation(row)
    if saved:
        _send_donation_emails_safe(saved)
        return _feed_item_from_row(saved)

    existing = get_donation_by_payment_intent(payment_intent.id)
    if existing:
        existing_status = str(existing.get("status") or "").lower()
        needs_upgrade = existing_status in {"failed", "pending", "processing", ""}
        needs_monthly = (
            row.get("frequency") == "monthly"
            and str(existing.get("frequency") or "").lower() != "monthly"
        )
        if needs_upgrade or needs_monthly or not existing.get("organization_id"):
            from db import rest_patch

            updates = {
                k: v
                for k, v in row.items()
                if v is not None and k != "stripe_payment_intent_id"
            }
            rest_patch("donations", updates, match={"id": existing["id"]})
            refreshed = get_donation_by_payment_intent(payment_intent.id) or {**existing, **updates}
            if needs_upgrade and str(refreshed.get("status") or "").lower() == "succeeded":
                _send_donation_emails_safe(refreshed)
            return _feed_item_from_row(refreshed)
        return _feed_item_from_row(existing)

    raise HTTPException(
        status_code=500,
        detail="Unable to save donation. Run backend/sql/004_add_base_amount.sql in Supabase, then retry.",
    )
