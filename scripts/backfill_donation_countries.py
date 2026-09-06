"""
Backfill script to populate/normalize 2-letter country codes on existing donations.
Surgically updates only the `device` column on rows where country was missing or unnormalized.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

# Add parent directory to path so imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from env_loader import load_app_env
load_app_env()

from db import rest_get, rest_patch
from paypal_client import get_paypal_order
from routers.admin_data import COUNTRY_NAME_TO_ISO_CODE, CURRENCY_TO_COUNTRY_FALLBACK


def run_backfill(limit: int = 1000):
    print("Starting donation country backfill...")
    accts = rest_get("paypal_accounts")
    paypal_client_id = None
    paypal_client_secret = None
    for a in accts:
        if a.get("client_id") and a.get("client_secret"):
            paypal_client_id = a["client_id"]
            paypal_client_secret = a["client_secret"]
            break

    # Fetch succeeded donations (most recent first)
    rows = rest_get(
        "donations",
        params={
            "status": "in.(succeeded,paid)",
            "order": "created_at.desc",
            "limit": str(limit),
        },
    )
    print(f"Fetched {len(rows)} donations to analyze.")

    updated_count = 0
    resolved_via_paypal = 0
    resolved_via_name_map = 0
    resolved_via_currency = 0

    paypal_cache: dict[str, str | None] = {}

    for row in rows:
        row_id = row.get("id")
        device = row.get("device")
        if not isinstance(device, dict):
            device = {}

        raw_country = (
            device.get("country")
            or device.get("Country")
            or device.get("country_code")
            or device.get("countryCode")
        )

        country_code: str | None = None

        # 1. Check if raw country exists and normalize it
        if raw_country:
            code_str = str(raw_country).strip().upper()
            if code_str in COUNTRY_NAME_TO_ISO_CODE:
                country_code = COUNTRY_NAME_TO_ISO_CODE[code_str]
                if country_code != raw_country:
                    resolved_via_name_map += 1
            elif len(code_str) == 2 and code_str.isalpha():
                country_code = "GB" if code_str == "UK" else code_str

        # 2. If no country, try PayPal API if it's a PayPal donation
        if not country_code:
            spi = str(row.get("stripe_payment_intent_id") or "")
            if "paypal:" in spi and paypal_client_id and paypal_client_secret:
                order_id = spi.replace("paypal:", "").strip()
                if order_id in paypal_cache:
                    country_code = paypal_cache[order_id]
                else:
                    try:
                        ord_data = get_paypal_order(
                            order_id,
                            client_id=paypal_client_id,
                            client_secret=paypal_client_secret,
                        )
                        ps = ord_data.get("payment_source") or {}
                        for source_key in ("apple_pay", "google_pay", "card", "paypal"):
                            src = ps.get(source_key) or {}
                            card = src.get("card") or {}
                            billing = card.get("billing_address") or src.get("address") or {}
                            cc = billing.get("country_code")
                            if cc and len(str(cc).strip()) == 2:
                                country_code = str(cc).strip().upper()
                                break
                        if not country_code:
                            payer = ord_data.get("payer") or {}
                            addr = payer.get("address") or {}
                            cc = addr.get("country_code")
                            if cc and len(str(cc).strip()) == 2:
                                country_code = str(cc).strip().upper()
                        paypal_cache[order_id] = country_code
                    except Exception:
                        paypal_cache[order_id] = None

                if country_code:
                    resolved_via_paypal += 1

        # 3. If still no country, check row country or fall back to currency
        if not country_code:
            row_c = row.get("country") or row.get("billing_country")
            if row_c:
                c_str = str(row_c).strip().upper()
                country_code = COUNTRY_NAME_TO_ISO_CODE.get(c_str, c_str if len(c_str) == 2 else None)

        if not country_code:
            cur = str(row.get("currency") or "").strip().upper()
            country_code = CURRENCY_TO_COUNTRY_FALLBACK.get(cur)
            if country_code:
                resolved_via_currency += 1

        # Update row in database if device changed
        if country_code and (device.get("country") != country_code):
            new_device = dict(device)
            new_device["country"] = country_code
            try:
                res = rest_patch("donations", {"device": new_device}, match={"id": str(row_id)})
                if res:
                    updated_count += 1
                else:
                    print(f"Patch returned None for donation {row_id}")
            except Exception as exc:
                print(f"Failed to patch donation {row_id}: {exc}")

    print("\nBackfill Complete!")
    print(f"  Total donations analyzed: {len(rows)}")
    print(f"  Total records patched: {updated_count}")
    print(f"    - Normalized country names: {resolved_via_name_map}")
    print(f"    - Resolved from PayPal API: {resolved_via_paypal}")
    print(f"    - Resolved from currency fallback: {resolved_via_currency}")


if __name__ == "__main__":
    limit_val = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    run_backfill(limit=limit_val)
