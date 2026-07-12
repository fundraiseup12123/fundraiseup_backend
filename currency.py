from __future__ import annotations

import math
import os
from typing import Literal

# Stripe zero-decimal currencies use whole units.
ZERO_DECIMAL = {
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf",
    "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
}

PAYPAL_CURRENCIES = {"usd", "eur", "gbp", "aud", "cad", "chf", "nzd", "sek", "dkk", "nok", "pln", "czk", "huf"}

# 1 USD = X PKR (override via PKR_USD_RATE in backend/.env)
PKR_USD_RATE = float(os.getenv("PKR_USD_RATE", "278"))

PaymentMethodType = Literal["card", "google_pay", "apple_pay", "paypal"]


def to_stripe_amount(amount: float, currency: str) -> int:
    code = currency.lower()
    if code in ZERO_DECIMAL:
        return int(round(amount))
    return int(round(amount * 100))


def from_stripe_amount(amount: int, currency: str) -> float:
    code = currency.lower()
    if code in ZERO_DECIMAL:
        return float(amount)
    return amount / 100


def format_display_amount(amount: float, currency: str) -> str:
    code = currency.upper()
    if code == "PKR":
        return f"Rs{amount:,.0f}"
    if code == "USD":
        return f"${amount:,.2f}" if amount % 1 else f"${amount:,.0f}"
    return f"{code} {amount:,.2f}"


def calculate_total_with_fees(amount: float, currency: str) -> float:
    """Gross-up donation so net after Stripe fees matches the intended gift."""
    code = currency.lower()
    if code in ZERO_DECIMAL:
        fixed = 0.0
        rate = 0.029
        gross = (amount + fixed) / (1 - rate)
        return float(math.ceil(gross))

    # PKR / USD style two-decimal currencies
    if code == "pkr":
        fixed = 0.0
        rate = 0.035
    else:
        fixed = 0.30
        rate = 0.029

    gross = (amount + fixed) / (1 - rate)
    return round(gross, 2)


def supports_paypal(currency: str) -> bool:
    return currency.lower() in PAYPAL_CURRENCIES


_EU_PAYPAL_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "IE", "IT",
    "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
}


def merchant_supports_stripe_paypal() -> bool:
    country = (
        os.getenv("STRIPE_MERCHANT_COUNTRY")
        or os.getenv("NEXT_PUBLIC_STRIPE_MERCHANT_COUNTRY")
        or "US"
    ).upper()
    return country in _EU_PAYPAL_COUNTRIES or country in {"GB", "UK", "CH", "LI", "NO"}


def paypal_available(display_currency: str) -> bool:
    from paypal_client import paypal_configured

    if paypal_configured():
        return True
    return merchant_supports_stripe_paypal() and supports_paypal(display_currency)


def paypal_checkout_currency() -> str:
    return os.getenv("PAYPAL_CURRENCY", "GBP").upper()


def pkr_to_usd(amount_pkr: float) -> float:
    return round(amount_pkr / PKR_USD_RATE, 2)


def charge_currency(display_currency: str, payment_method: PaymentMethodType) -> str:
    """PayPal via Stripe charges in USD when the display currency is not PayPal-supported."""
    if payment_method == "paypal" and not supports_paypal(display_currency):
        return "usd"
    return display_currency.lower()


def convert_for_charge(total_amount: float, display_currency: str, payment_method: PaymentMethodType) -> float:
    if payment_method == "paypal" and not supports_paypal(display_currency):
        code = display_currency.lower()
        if code == "pkr":
            return pkr_to_usd(total_amount)
        usd_equivalent = total_amount / usd_rate(display_currency)
        return round(usd_equivalent, 2)
    return total_amount


def conversion_note(display_currency: str, payment_method: PaymentMethodType, total_display: float) -> str | None:
    from paypal_client import paypal_configured

    if payment_method == "paypal" and paypal_configured():
        charge_currency_code, charge_total = convert_for_paypal(total_display, display_currency)
        if charge_currency_code != display_currency.upper():
            return (
                f"PayPal charges in {charge_currency_code}. Your "
                f"{format_display_amount(total_display, display_currency)} donation is approximately "
                f"{format_display_amount(charge_total, charge_currency_code)}."
            )
        return None

    if payment_method == "paypal" and not supports_paypal(display_currency):
        charge_total = convert_for_charge(total_display, display_currency, payment_method)
        return (
            f"PayPal charges in USD. Your {format_display_amount(total_display, display_currency)} "
            f"donation is approximately {format_display_amount(charge_total, 'usd')}."
        )
    return None


# 1 USD expressed in local currency (approximate rates for admin reporting).
USD_TO_LOCAL: dict[str, float] = {
    "USD": 1.0,
    "GBP": 0.79,
    "EUR": 0.92,
    "PKR": PKR_USD_RATE,
    "INR": 83.5,
    "AED": 3.67,
    "AUD": 1.55,
    "CAD": 1.36,
    "CHF": 0.88,
    "CNY": 7.25,
    "JPY": 150.0,
    "SAR": 3.75,
    "SGD": 1.34,
    "TWD": 32.0,
    "ZAR": 18.5,
    "NGN": 1550.0,
    "KES": 129.0,
    "BRL": 5.0,
    "MXN": 17.0,
    "TRY": 32.0,
    "PLN": 4.0,
    "SEK": 10.5,
    "NOK": 10.8,
    "DKK": 6.9,
    "NZD": 1.65,
    "HKD": 7.8,
    "THB": 36.0,
    "IDR": 15800.0,
    "PHP": 56.0,
    "MYR": 4.7,
    "EGP": 49.0,
    "ILS": 3.7,
    "KRW": 1340.0,
}


def usd_rate(currency: str) -> float:
    return USD_TO_LOCAL.get(currency.upper(), 1.0)


def convert_to_reporting(amount: float, from_currency: str, reporting_currency: str) -> float:
    from_code = from_currency.upper()
    to_code = reporting_currency.upper()
    if from_code == to_code:
        return round(amount, 2)
    usd_equivalent = amount / usd_rate(from_code)
    converted = usd_equivalent * usd_rate(to_code)
    if to_code in {c.upper() for c in ZERO_DECIMAL}:
        return float(round(converted))
    return round(converted, 2)


def convert_min_donation(min_amount: float, from_currency: str, to_currency: str) -> float:
    """Convert a campaign minimum into the donor currency (ceil so never under)."""
    from_code = from_currency.upper()
    to_code = to_currency.upper()
    if min_amount <= 0:
        return 0.0
    if from_code == to_code:
        return float(min_amount)
    usd_equivalent = float(min_amount) / usd_rate(from_code)
    raw = usd_equivalent * usd_rate(to_code)
    if to_code in {c.upper() for c in ZERO_DECIMAL}:
        return float(math.ceil(raw))
    return math.ceil(raw * 100) / 100


def assert_meets_min_donation(
    amount: float,
    currency: str,
    *,
    min_donation_amount: float | None,
    default_currency: str | None,
) -> None:
    """Raise HTTPException if amount is below the campaign minimum."""
    from fastapi import HTTPException

    if min_donation_amount is None:
        return
    try:
        min_val = float(min_donation_amount)
    except (TypeError, ValueError):
        return
    if min_val <= 0:
        return
    base = (default_currency or "USD").upper()
    donor = (currency or "USD").upper()
    required = convert_min_donation(min_val, base, donor)
    if amount + 1e-9 < required:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum donation is {format_display_amount(required, donor)}",
        )


def convert_for_paypal(total_amount: float, display_currency: str) -> tuple[str, float]:
    target = paypal_checkout_currency()
    if display_currency.upper() == target:
        return target, round(total_amount, 2)
    converted = convert_to_reporting(total_amount, display_currency, target)
    return target, round(converted, 2)


def estimate_processing_fee(amount: float, currency: str) -> float:
    """Estimated Stripe fee for a gift amount (before gross-up)."""
    code = currency.lower()
    if code == "pkr":
        gross = math.ceil(amount / (1 - 0.035))
        return float(round(gross - amount))
    gross = round((amount + 0.30) / (1 - 0.029), 2)
    return round(gross - amount, 2)
