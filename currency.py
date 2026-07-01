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

PaymentMethodType = Literal["card", "google_pay", "paypal"]


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


def pkr_to_usd(amount_pkr: float) -> float:
    return round(amount_pkr / PKR_USD_RATE, 2)


def charge_currency(display_currency: str, payment_method: PaymentMethodType) -> str:
    """Charge in the donor's selected currency for all payment methods."""
    _ = payment_method
    return display_currency.lower()


def convert_for_charge(total_amount: float, display_currency: str, payment_method: PaymentMethodType) -> float:
    _ = display_currency, payment_method
    return total_amount


def conversion_note(display_currency: str, payment_method: PaymentMethodType, total_display: float) -> str | None:
    _ = display_currency, payment_method, total_display
    return None
