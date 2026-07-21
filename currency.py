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
NOWPAYMENTS_FIAT = {"usd", "eur", "gbp", "aud", "cad", "chf", "nzd", "sek", "dkk", "nok", "pln", "czk", "huf", "jpy", "ils", "inr", "aed", "sgd", "hkd", "try", "brl", "mxn"}

# 1 USD = X PKR (override via PKR_USD_RATE in backend/.env)
PKR_USD_RATE = float(os.getenv("PKR_USD_RATE", "278"))

PaymentMethodType = Literal["card", "google_pay", "apple_pay", "paypal", "nowpayments"]


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

    if payment_method == "nowpayments":
        charge_code, charge_total = convert_for_nowpayments(total_display, display_currency)
        if charge_code != display_currency.upper():
            return (
                f"Crypto checkout prices in {charge_code}. Your "
                f"{format_display_amount(total_display, display_currency)} donation is approximately "
                f"{format_display_amount(charge_total, charge_code)}."
            )
        return (
            "You will complete payment in crypto on NOWPayments. "
            "The crypto amount is calculated at checkout."
        )

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
# Keep in sync with src/lib/currency.ts USD_TO_LOCAL — missing codes fall back to 1.0 and break conversion.
USD_TO_LOCAL: dict[str, float] = {
    "AED": 3.67,
    "AFN": 71.0,
    "ALL": 95.0,
    "AMD": 390.0,
    "ANG": 1.79,
    "AOA": 850.0,
    "ARS": 900.0,
    "AUD": 1.55,
    "AWG": 1.79,
    "AZN": 1.7,
    "BAM": 1.8,
    "BBD": 2.0,
    "BDT": 110.0,
    "BGN": 1.8,
    "BHD": 0.376,
    "BIF": 2900.0,
    "BMD": 1.0,
    "BND": 1.34,
    "BOB": 6.9,
    "BRL": 5.0,
    "BSD": 1.0,
    "BWP": 13.6,
    "BYN": 3.3,
    "BZD": 2.0,
    "CAD": 1.36,
    "CDF": 2800.0,
    "CHF": 0.88,
    "CLP": 950.0,
    "CNY": 7.25,
    "COP": 4100.0,
    "CRC": 520.0,
    "CVE": 102.0,
    "CZK": 23.0,
    "DJF": 178.0,
    "DKK": 6.9,
    "DOP": 59.0,
    "DZD": 134.0,
    "EGP": 49.0,
    "ETB": 57.0,
    "EUR": 0.92,
    "FJD": 2.25,
    "FKP": 0.79,
    "GBP": 0.79,
    "GEL": 2.7,
    "GIP": 0.79,
    "GMD": 68.0,
    "GNF": 8600.0,
    "GTQ": 7.8,
    "GYD": 209.0,
    "HKD": 7.8,
    "HNL": 24.7,
    "HRK": 7.0,
    "HTG": 132.0,
    "HUF": 360.0,
    "IDR": 15800.0,
    "ILS": 3.7,
    "INR": 83.5,
    "ISK": 138.0,
    "JMD": 156.0,
    "JOD": 0.71,
    "JPY": 150.0,
    "KES": 129.0,
    "KGS": 89.0,
    "KHR": 4100.0,
    "KMF": 460.0,
    "KRW": 1340.0,
    "KWD": 0.31,
    "KYD": 0.83,
    "KZT": 450.0,
    "LAK": 21000.0,
    "LBP": 89000.0,
    "LKR": 300.0,
    "LRD": 195.0,
    "LSL": 18.5,
    "MAD": 10.0,
    "MDL": 18.0,
    "MGA": 4500.0,
    "MKD": 57.0,
    "MMK": 2100.0,
    "MNT": 3400.0,
    "MOP": 8.0,
    "MUR": 46.0,
    "MVR": 15.4,
    "MWK": 1700.0,
    "MXN": 17.0,
    "MYR": 4.7,
    "MZN": 64.0,
    "NAD": 18.5,
    "NGN": 1600.0,
    "NIO": 36.7,
    "NOK": 10.8,
    "NPR": 133.0,
    "NZD": 1.67,
    "OMR": 0.384,
    "PAB": 1.0,
    "PEN": 3.75,
    "PGK": 3.9,
    "PHP": 56.0,
    "PKR": PKR_USD_RATE,
    "PLN": 4.0,
    "PYG": 7300.0,
    "QAR": 3.64,
    "RON": 4.6,
    "RSD": 108.0,
    "RUB": 92.0,
    "RWF": 1300.0,
    "SAR": 3.75,
    "SBD": 8.4,
    "SCR": 13.5,
    "SEK": 10.5,
    "SGD": 1.34,
    "SHP": 0.79,
    "SLE": 22.0,
    "SOS": 571.0,
    "SRD": 35.0,
    "STN": 23.0,
    "SVC": 8.75,
    "SZL": 18.5,
    "THB": 36.0,
    "TJS": 10.9,
    "TND": 3.1,
    "TOP": 2.4,
    "TRY": 32.0,
    "TTD": 6.8,
    "TWD": 32.0,
    "TZS": 2600.0,
    "UAH": 41.0,
    "UGX": 3800.0,
    "USD": 1.0,
    "UYU": 39.0,
    "UZS": 12600.0,
    "VND": 24500.0,
    "VUV": 120.0,
    "WST": 2.7,
    "XAF": 610.0,
    "XCD": 2.7,
    "XOF": 610.0,
    "XPF": 110.0,
    "YER": 250.0,
    "ZAR": 18.5,
    "ZMW": 27.0,
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


def resolve_min_donation_for_frequency(
    campaign: dict,
    frequency: str,
) -> float | None:
    """Pick once/monthly minimum; fall back to legacy min_donation_amount."""
    freq = (frequency or "once").lower()
    key = "min_donation_amount_monthly" if freq == "monthly" else "min_donation_amount_once"
    raw = campaign.get(key)
    if raw is None:
        raw = campaign.get("min_donation_amount")
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


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
            detail=f"Minimum {format_display_amount(required, donor)}",
        )




def supports_nowpayments_fiat(currency: str) -> bool:
    return currency.lower() in NOWPAYMENTS_FIAT


def convert_for_nowpayments(total_amount: float, display_currency: str) -> tuple[str, float]:
    """Invoice fiat NOWPayments can price — map unsupported (e.g. PKR) to USD."""
    code = display_currency.upper()
    if supports_nowpayments_fiat(code):
        return code, round(float(total_amount), 2)
    converted = convert_to_reporting(float(total_amount), code, "USD")
    return "USD", round(converted, 2)

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
