from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=r"https://.*\.(up\.railway\.app|vercel\.app)|https://.*\.ngrok-free\.(app|dev)|https://.*\.ngrok\.io",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class DonorDetails(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=254)
    phone: str | None = None


class UtmParams(BaseModel):
    source: str | None = None
    medium: str | None = None
    campaign: str | None = None
    term: str | None = None
    content: str | None = None


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
    checkout_view: Literal["homepage", "popup"] = "homepage"
    utm: UtmParams | None = None


class SwitchPaymentMethodRequest(BaseModel):
    payment_method: PaymentMethodType
    cover_fees: bool = False


class UpdateCheckoutRequest(BaseModel):
    cover_fees: bool


class RegisterDomainRequest(BaseModel):
    domain: str = Field(min_length=3, max_length=253)


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


class DonationFeedResponse(BaseModel):
    donations: list[DonationFeedItem]
    has_more: bool


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

    try:
        existing_domains = stripe.PaymentMethodDomain.list(limit=100)
        for item in existing_domains.data:
            if item.domain_name == domain:
                return WalletDomainResponse(
                    domain=domain,
                    registered=True,
                    google_pay_status=getattr(item.google_pay, "status", None),
                    apple_pay_status=getattr(item.apple_pay, "status", None),
                )

        created = stripe.PaymentMethodDomain.create(domain_name=domain)
        return WalletDomainResponse(
            domain=domain,
            registered=True,
            created=True,
            google_pay_status=getattr(created.google_pay, "status", None),
            apple_pay_status=getattr(created.apple_pay, "status", None),
        )
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
) -> dict[str, str]:
    meta = {
        "first_name": payload.donor.first_name,
        "last_name": payload.donor.last_name,
        "email": payload.donor.email,
        "phone": payload.donor.phone or "",
        "frequency": payload.frequency,
        "dedicate": str(payload.dedicate).lower(),
        "honoree_name": (payload.honoree_name or "")[:500],
        "comment": (payload.comment or "")[:500],
        "base_amount": str(base_amount),
        "cover_fees": str(payload.cover_fees).lower(),
        "campaign": campaign_slug or "sdnemergency",
        "display_currency": payload.currency.upper(),
        "payment_method": payment_method,
    }
    if organization_id:
        meta["organization_id"] = organization_id
    if campaign_id:
        meta["campaign_id"] = campaign_id
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
    receipt_email: str,
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
        "receipt_email": receipt_email,
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
    if isinstance(invoice, str):
        invoice = stripe.Invoice.retrieve(
            invoice,
            expand=["confirmation_secret", "payments.data.payment.payment_intent", "payment_intent"],
            **retrieve_kwargs,
        )

    if not invoice:
        raise HTTPException(status_code=500, detail="Unable to create subscription payment")

    confirmation = getattr(invoice, "confirmation_secret", None)
    if confirmation and getattr(confirmation, "client_secret", None):
        return confirmation.client_secret, _payment_intent_id_from_invoice(invoice)

    payment_intent = getattr(invoice, "payment_intent", None)
    if payment_intent:
        if isinstance(payment_intent, str):
            payment_intent = stripe.PaymentIntent.retrieve(payment_intent, **retrieve_kwargs)
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
) -> CheckoutResponse:
    charge_curr = charge_currency(display_currency, payment_method)
    charge_total = convert_for_charge(total_display, display_currency, payment_method)
    resolved_secret = client_secret or (payment_intent.client_secret if payment_intent else None)
    resolved_payment_intent_id = payment_intent_id or (payment_intent.id if payment_intent else None)

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
    )


def _intent_metadata(payment_intent: stripe.PaymentIntent) -> dict[str, str]:
    raw = payment_intent.metadata
    if not raw:
        return {}
    return raw.to_dict()


def _payload_from_intent(existing: stripe.PaymentIntent, payment_method: PaymentMethodType, cover_fees: bool) -> CreateCheckoutRequest:
    meta = _intent_metadata(existing)
    display_currency = meta.get("display_currency", existing.currency.upper())
    base_amount = float(meta.get("base_amount", from_stripe_amount(existing.amount, existing.currency)))

    return CreateCheckoutRequest(
        amount=base_amount,
        currency=display_currency,
        frequency=meta.get("frequency", "once"),  # type: ignore[arg-type]
        cover_fees=cover_fees,
        dedicate=meta.get("dedicate", "false").lower() == "true",
        honoree_name=meta.get("honoree_name") or None,
        comment=meta.get("comment") or None,
        payment_method=payment_method,
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
    from site_constants import ROOT_CAMPAIGN_ID

    display_currency = payload.currency.lower()
    payment_method = payload.payment_method
    organization_id: str | None = None
    campaign_slug: str | None = None
    stripe_account: str | None = None

    if payload.campaign_id and _is_uuid(payload.campaign_id):
        campaign = rest_get_one(
            "campaigns",
            params={"id": f"eq.{payload.campaign_id}", "select": "id,organization_id,slug,status"},
        )
        if campaign:
            if campaign.get("status") != "live":
                raise HTTPException(status_code=400, detail="Campaign is not available for checkout")
            organization_id = campaign["organization_id"]
            campaign_slug = campaign["slug"]
            stripe_account, _ = resolve_stripe_account_for_checkout(organization_id, payload.campaign_id)
        elif payload.campaign_id != ROOT_CAMPAIGN_ID:
            raise HTTPException(status_code=400, detail="Campaign is not available for checkout")

    if not stripe_account and (
        not payload.campaign_id or payload.campaign_id == ROOT_CAMPAIGN_ID
    ):
        from routers.payment_accounts import resolve_root_stripe_account

        stripe_account = resolve_root_stripe_account(payload.checkout_view)

    base_amount, total_display = _resolve_amounts(payload.amount, display_currency, payload.cover_fees)
    metadata = _checkout_metadata(
        payload,
        base_amount,
        payment_method,
        organization_id=organization_id,
        campaign_id=payload.campaign_id,
        campaign_slug=campaign_slug,
    )

    try:
        customer_kwargs: dict = {
            "email": payload.donor.email,
            "name": f"{payload.donor.first_name} {payload.donor.last_name}",
            "phone": payload.donor.phone,
            "metadata": metadata,
        }
        if stripe_account:
            customer_kwargs["stripe_account"] = stripe_account
        customer = stripe.Customer.create(**customer_kwargs)

        if payload.frequency == "monthly":
            charge_curr = charge_currency(display_currency, payment_method)
            charge_total = convert_for_charge(total_display, display_currency, payment_method)
            stripe_amount = to_stripe_amount(charge_total, charge_curr)

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
                "customer": customer.id,
                "items": [{"price": price.id}],
                "payment_behavior": "default_incomplete",
                "payment_settings": {
                    "payment_method_types": _payment_method_types(charge_curr, payment_method),
                    "save_default_payment_method": "on_subscription",
                },
                "expand": ["latest_invoice.confirmation_secret"],
                "metadata": metadata,
            }
            if stripe_account:
                sub_kwargs["stripe_account"] = stripe_account
            subscription = stripe.Subscription.create(**sub_kwargs)
            client_secret, payment_intent_id = _subscription_payment_details(
                subscription,
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
            )

        payment_intent = _create_once_payment_intent(
            display_currency=display_currency,
            payment_method=payment_method,
            base_amount=base_amount,
            total_display=total_display,
            cover_fees=payload.cover_fees,
            customer_id=customer.id,
            receipt_email=payload.donor.email,
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
    try:
        existing = stripe.PaymentIntent.retrieve(payment_intent_id)
        if existing.status in {"canceled", "succeeded"}:
            raise HTTPException(status_code=400, detail="This payment session is no longer active.")

        meta = _intent_metadata(existing)
        display_currency = meta.get("display_currency", existing.currency.upper()).lower()
        payment_method = payload.payment_method

        if payment_method == "paypal" and not supports_paypal(display_currency):
            raise HTTPException(status_code=400, detail="PayPal is only available for USD donations.")

        checkout_payload = _payload_from_intent(existing, payment_method, payload.cover_fees)
        base_amount, total_display = _resolve_amounts(
            checkout_payload.amount,
            display_currency,
            payload.cover_fees,
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
        )

        frequency = meta.get("frequency", "once")
        return _build_checkout_response(
            payment_intent=payment_intent,
            display_currency=display_currency,
            payment_method=payment_method,
            base_amount=base_amount,
            total_display=total_display,
            frequency=frequency if frequency in {"once", "monthly"} else "once",
        )
    except HTTPException:
        raise
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=400, detail=str(exc.user_message or exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.patch("/checkout/{payment_intent_id}", response_model=CheckoutResponse)
def update_checkout(payment_intent_id: str, payload: UpdateCheckoutRequest) -> CheckoutResponse:
    try:
        existing = stripe.PaymentIntent.retrieve(payment_intent_id)
        meta = _intent_metadata(existing)
        display_currency = meta.get("display_currency", existing.currency.upper()).lower()
        payment_method: PaymentMethodType = meta.get("payment_method", "card")  # type: ignore[assignment]
        base_amount = float(meta.get("base_amount", from_stripe_amount(existing.amount, existing.currency)))
        _, total_display = _resolve_amounts(base_amount, display_currency, payload.cover_fees)
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
        )

        frequency = meta.get("frequency", "once")
        return _build_checkout_response(
            payment_intent=updated,
            display_currency=display_currency,
            payment_method=payment_method,
            base_amount=base_amount,
            total_display=total_display,
            frequency=frequency if frequency in {"once", "monthly"} else "once",
        )
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=400, detail=str(exc.user_message or exc)) from exc


def _metadata_from_payment_intent(payment_intent: stripe.PaymentIntent) -> dict[str, str]:
    meta = _intent_metadata(payment_intent)
    if meta.get("first_name"):
        return meta

    invoice_id = payment_intent.invoice
    if not invoice_id:
        return meta

    invoice_id_str = invoice_id if isinstance(invoice_id, str) else invoice_id.id
    invoice = stripe.Invoice.retrieve(invoice_id_str, expand=["subscription"])
    subscription = invoice.subscription
    if subscription and not isinstance(subscription, str):
        sub_meta = subscription.metadata.to_dict() if subscription.metadata else {}
        return {**meta, **sub_meta}
    return meta


def _donation_row_from_intent(payment_intent: stripe.PaymentIntent) -> dict[str, str | float | None | dict[str, str]]:
    meta = _metadata_from_payment_intent(payment_intent)
    display_currency = meta.get("display_currency", payment_intent.currency.upper()).upper()
    total_display = float(meta.get("total_display", from_stripe_amount(payment_intent.amount, payment_intent.currency)))
    frequency = meta.get("frequency", "once")
    if frequency not in {"once", "monthly"}:
        frequency = "once"

    base_amount = float(meta.get("base_amount", total_display))
    cover_fees = meta.get("cover_fees", "false").lower() == "true"
    if cover_fees:
        processing_fee = max(0.0, round(total_display - base_amount, 2))
        payout_amount = base_amount
    else:
        processing_fee = estimate_processing_fee(base_amount, display_currency)
        payout_amount = max(0.0, round(base_amount - processing_fee, 2))

    row: dict[str, str | float | None | dict[str, str] | bool] = {
        "stripe_payment_intent_id": payment_intent.id,
        "first_name": meta.get("first_name", "Anonymous"),
        "last_name": meta.get("last_name", ""),
        "email": meta.get("email") or None,
        "amount": total_display,
        "base_amount": base_amount,
        "currency": display_currency,
        "frequency": frequency,
        "payment_method": meta.get("payment_method"),
        "honoree_name": meta.get("honoree_name") or None,
        "comment": meta.get("comment") or None,
        "organization_id": meta.get("organization_id"),
        "campaign_id": meta.get("campaign_id"),
        "status": "succeeded",
        "fee_covered": cover_fees,
        "platform_fee": 0,
        "processing_fee": processing_fee,
        "payout_amount": payout_amount,
    }
    utm = _utm_from_meta(meta)
    if utm:
        row["utm"] = utm
    return row


from routers.super_admin import router as super_admin_router
from routers.organizations import router as organizations_router
from routers.public import router as public_router
from routers.stripe_connect import router as stripe_router
from routers.invites import router as invites_router
from routers.admin_data import router as admin_data_router
from routers.paypal import router as paypal_router
from routers.paypal_connect import router as paypal_connect_router
from routers.payment_accounts import router as payment_accounts_router
from routers.emails import router as emails_router

app.include_router(super_admin_router)
app.include_router(payment_accounts_router)
app.include_router(organizations_router)
app.include_router(public_router)
app.include_router(stripe_router)
app.include_router(invites_router)
app.include_router(admin_data_router)
app.include_router(paypal_router)
app.include_router(paypal_connect_router)
app.include_router(emails_router)


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
        meta = pi.get("metadata", {})
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

        row = {
            "stripe_payment_intent_id": pi["id"],
            "first_name": meta.get("first_name", "Anonymous"),
            "last_name": meta.get("last_name", ""),
            "email": meta.get("email"),
            "amount": total_display,
            "base_amount": base_amount,
            "currency": display_currency,
            "frequency": meta.get("frequency", "once"),
            "payment_method": meta.get("payment_method"),
            "honoree_name": meta.get("honoree_name"),
            "comment": meta.get("comment"),
            "status": "succeeded",
            "organization_id": meta.get("organization_id"),
            "campaign_id": meta.get("campaign_id"),
            "stripe_account_id": pi.get("on_behalf_of") or (pi.get("transfer_data") or {}).get("destination"),
            "fee_covered": cover_fees,
            "platform_fee": 0,
            "processing_fee": processing_fee,
            "payout_amount": payout_amount,
        }
        utm = _utm_from_meta(meta)
        if utm:
            row["utm"] = utm
        saved = insert_donation({k: v for k, v in row.items() if v is not None})
        if saved:
            from emails import send_donation_confirmation_for_row

            send_donation_confirmation_for_row(saved)

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
) -> DonationFeedResponse:
    if limit < 1 or limit > 50:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 50")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")

    if not supabase_enabled():
        return DonationFeedResponse(donations=[], has_more=False)

    from db import rest_get as db_rest_get

    if campaign_id and _is_uuid(campaign_id) and supabase_enabled():
        params = {
            "campaign_id": f"eq.{campaign_id}",
            "select": "id,first_name,last_name,amount,currency,frequency,honoree_name,created_at",
            "order": "created_at.desc",
            "limit": str(limit + 1),
            "offset": str(offset),
        }
        rows = db_rest_get("donations", params=params)
    else:
        rows = list_donations(limit=limit + 1, offset=offset)
    has_more = len(rows) > limit
    visible = rows[:limit]

    donations = [
        DonationFeedItem(
            id=str(row["id"]),
            first_name=row["first_name"],
            last_name=row["last_name"],
            amount=float(row["amount"]),
            currency=str(row["currency"]).upper(),
            frequency=row["frequency"] if row["frequency"] in {"once", "monthly"} else "once",
            honoree_name=row.get("honoree_name"),
            created_at=row["created_at"],
        )
        for row in visible
    ]
    return DonationFeedResponse(donations=donations, has_more=has_more)


@app.post("/donations/record", response_model=DonationFeedItem)
def record_donation(payload: RecordDonationRequest) -> DonationFeedItem:
    if not supabase_enabled():
        raise HTTPException(status_code=503, detail="Donation storage is not configured")

    try:
        payment_intent = stripe.PaymentIntent.retrieve(payload.payment_intent_id)
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=400, detail=str(exc.user_message or exc)) from exc

    if payment_intent.status != "succeeded":
        raise HTTPException(status_code=400, detail="Payment has not succeeded yet")

    row = _donation_row_from_intent(payment_intent)
    saved = insert_donation(row)
    if saved:
        from emails import send_donation_confirmation_for_row

        send_donation_confirmation_for_row(saved)
        return DonationFeedItem(
            id=str(saved["id"]),
            first_name=saved["first_name"],
            last_name=saved["last_name"],
            amount=float(saved["amount"]),
            currency=str(saved["currency"]).upper(),
            frequency=saved["frequency"] if saved["frequency"] in {"once", "monthly"} else "once",
            honoree_name=saved.get("honoree_name"),
            created_at=saved["created_at"],
        )

    existing = get_donation_by_payment_intent(payment_intent.id)
    if existing:
        return DonationFeedItem(
            id=str(existing["id"]),
            first_name=existing["first_name"],
            last_name=existing["last_name"],
            amount=float(existing["amount"]),
            currency=str(existing["currency"]).upper(),
            frequency=existing["frequency"] if existing["frequency"] in {"once", "monthly"} else "once",
            honoree_name=existing.get("honoree_name"),
            created_at=existing["created_at"],
        )

    raise HTTPException(
        status_code=500,
        detail="Unable to save donation. Run backend/sql/004_add_base_amount.sql in Supabase, then retry.",
    )
