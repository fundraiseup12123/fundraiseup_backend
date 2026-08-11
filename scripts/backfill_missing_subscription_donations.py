"""Backfill donation rows for active Stripe subscriptions missing from donations."""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Allow running as `python scripts/...` from backend/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stripe  # noqa: E402

from db import rest_get  # noqa: E402
from main import (  # noqa: E402
    _donation_row_from_intent,
    _ensure_donation_org,
    _payment_intent_id_from_invoice,
)
from stripe_intents import retrieve_payment_intent  # noqa: E402
from supabase_client import get_donation_by_payment_intent, insert_donation  # noqa: E402

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")


def _pi_for_subscription(sub: stripe.Subscription, stripe_account: str | None) -> str | None:
    kwargs = {"stripe_account": stripe_account} if stripe_account else {}
    inv = sub.latest_invoice
    if isinstance(inv, str):
        inv = stripe.Invoice.retrieve(
            inv,
            expand=["confirmation_secret", "payments.data.payment.payment_intent", "payment_intent"],
            **kwargs,
        )
    pi_id = _payment_intent_id_from_invoice(inv) if inv else None
    if pi_id:
        return pi_id
    conf = getattr(inv, "confirmation_secret", None) if inv else None
    secret = getattr(conf, "client_secret", None) if conf else None
    if secret and secret.startswith("pi_") and "_secret_" in secret:
        return secret.split("_secret_", 1)[0]
    return None


def backfill_account(stripe_account: str | None, *, limit: int = 30) -> int:
    label = stripe_account or "platform"
    kwargs: dict = {"limit": limit, "status": "active"}
    if stripe_account:
        kwargs["stripe_account"] = stripe_account
    subs = stripe.Subscription.list(**kwargs)
    fixed = 0
    for sub in subs.data:
        pi_id = _pi_for_subscription(sub, stripe_account)
        if not pi_id:
            print(f"[{label}] {sub.id}: no payment intent")
            continue
        existing = get_donation_by_payment_intent(pi_id)
        if existing and str(existing.get("status") or "").lower() == "succeeded":
            freq = str(existing.get("frequency") or "").lower()
            if freq == "monthly" and existing.get("stripe_subscription_id"):
                print(f"[{label}] {sub.id}: already recorded {existing.get('id')}")
                continue
        try:
            payment_intent, acct = retrieve_payment_intent(
                pi_id,
                stripe_account=stripe_account,
                expand=["payment_method", "latest_charge"],
            )
            row = _ensure_donation_org(
                _donation_row_from_intent(payment_intent, stripe_account=acct)
            )
            row["frequency"] = "monthly"
            row["stripe_subscription_id"] = sub.id
            if existing:
                from db import rest_patch

                updates = {
                    k: v
                    for k, v in row.items()
                    if v is not None and k != "stripe_payment_intent_id"
                }
                rest_patch("donations", updates, match={"id": existing["id"]})
                saved = get_donation_by_payment_intent(pi_id) or {**existing, **updates}
                print(f"[{label}] {sub.id}: patched {saved.get('id')} freq={saved.get('frequency')}")
            else:
                saved = insert_donation(row)
                if not saved:
                    print(f"[{label}] {sub.id}: insert failed for {pi_id}")
                    continue
                print(
                    f"[{label}] {sub.id}: inserted {saved.get('id')} "
                    f"{saved.get('amount')} {saved.get('currency')} org={saved.get('organization_id')}"
                )
            fixed += 1
        except Exception as exc:
            print(f"[{label}] {sub.id}: ERROR {exc}")
    return fixed


def main() -> None:
    accounts = [None]
    for row in rest_get("stripe_accounts", params={"select": "stripe_account_id", "limit": "50"}) or []:
        acct = row.get("stripe_account_id")
        if acct:
            accounts.append(str(acct))
    total = 0
    for acct in accounts:
        total += backfill_account(acct)
    print(f"done fixed={total}")


if __name__ == "__main__":
    main()
