"""
Migration script:
1. All organizations & campaigns: Once mode payments (Card, Apple Pay, Google Pay) -> PayPal.
2. All organizations & campaigns: Monthly donations -> New Stripe account acct_1U5lOf05r8OdmItt (id: 8386ccdc-dc46-470b-ad9f-bfd6cc7e5e3a).
3. Updates platform payment_accounts_json pool default to the new account.
"""

import json
import logging
from typing import Any

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import rest_get, rest_get_one, rest_patch
from site_constants import ROOT_CAMPAIGN_ID

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migrate_payment_routing")

NEW_STRIPE_ACCOUNT_ID = "acct_1U5lOf05r8OdmItt"
NEW_STRIPE_ENTRY_ID = "8386ccdc-dc46-470b-ad9f-bfd6cc7e5e3a"
HOPE_FOR_GAZA_STRIPE_ORG_ID = "2b395297-6428-49f3-b125-0cca9bbd1256"


def update_platform_payment_accounts_json():
    logger.info("Updating platform payment_accounts_json on root campaign %s...", ROOT_CAMPAIGN_ID)
    row = rest_get_one(
        "campaign_content",
        params={"campaign_id": f"eq.{ROOT_CAMPAIGN_ID}", "select": "payment_accounts_json"},
    )
    if not row:
        logger.error("Root campaign content not found!")
        return

    raw = row.get("payment_accounts_json")
    accounts = json.loads(raw) if isinstance(raw, str) else (raw or {})

    pool = accounts.get("stripe_accounts")
    if not isinstance(pool, list):
        pool = []
        accounts["stripe_accounts"] = pool

    # Ensure new account entry is present in pool and marked as default
    found = False
    for entry in pool:
        if isinstance(entry, dict):
            if entry.get("stripe_account_id") == NEW_STRIPE_ACCOUNT_ID or entry.get("id") == NEW_STRIPE_ENTRY_ID:
                entry["id"] = NEW_STRIPE_ENTRY_ID
                entry["stripe_account_id"] = NEW_STRIPE_ACCOUNT_ID
                entry["name"] = "Hope For Gaza Stripe (New)"
                entry["connection_status"] = "active"
                entry["charges_enabled"] = True
                entry["is_default"] = True
                found = True
            else:
                entry["is_default"] = False

    if not found:
        pool.append({
            "id": NEW_STRIPE_ENTRY_ID,
            "name": "Hope For Gaza Stripe (New)",
            "stripe_account_id": NEW_STRIPE_ACCOUNT_ID,
            "connection_status": "active",
            "charges_enabled": True,
            "is_default": True,
        })

    # Update homepage, popup, landing to reference new account
    for view in ("homepage", "popup", "landing"):
        view_data = accounts.setdefault(view, {})
        view_data["stripe_account_id"] = NEW_STRIPE_ACCOUNT_ID
        view_data["stripe_connection_status"] = "active"
        view_data["stripe_charges_enabled"] = True

    # Set platform default main payment processor to paypal
    accounts["default_payment_processor"] = "paypal"

    rest_patch(
        "campaign_content",
        {"payment_accounts_json": json.dumps(accounts)},
        match={"campaign_id": ROOT_CAMPAIGN_ID},
    )
    logger.info("Updated platform payment_accounts_json successfully.")


def update_organizations():
    logger.info("Updating organizations...")
    orgs = rest_get("organizations", params={"select": "id,name,payment_processor,payment_account_sources"})
    for org in orgs:
        org_id = org["id"]
        sources = dict(org.get("payment_account_sources") or {})
        
        # Stripe source is platform (except Hope For Gaza Stripe org which connects its own org account)
        if org_id == HOPE_FOR_GAZA_STRIPE_ORG_ID:
            sources["stripe"] = "organization"
        else:
            sources["stripe"] = "platform"

        # Update organization
        rest_patch(
            "organizations",
            {
                "payment_processor": "paypal",
                "payment_account_sources": sources,
            },
            match={"id": org_id},
        )
        logger.info("Updated organization %s (%s) -> payment_processor=paypal, stripe=%s", org["name"], org_id, sources["stripe"])


def update_campaigns():
    logger.info("Updating campaigns...")
    campaigns = rest_get(
        "campaigns",
        params={"select": "id,name,organization_id,payment_processor,payment_account_sources,stripe_account_id,platform_stripe_account_id"},
    )
    for c in campaigns:
        cid = c["id"]
        updates: dict[str, Any] = {
            "stripe_account_id": NEW_STRIPE_ENTRY_ID,
            "platform_stripe_account_id": NEW_STRIPE_ENTRY_ID,
        }

        # If payment_processor was stripe or null, switch to paypal
        if c.get("payment_processor") in (None, "", "stripe"):
            updates["payment_processor"] = "paypal"

        sources = dict(c.get("payment_account_sources") or {})
        sources["stripe_new_account_id"] = NEW_STRIPE_ENTRY_ID
        updates["payment_account_sources"] = sources

        rest_patch("campaigns", updates, match={"id": cid})
        logger.info("Updated campaign '%s' (%s): processor=%s, stripe=%s", c.get("name"), cid, updates.get("payment_processor", c.get("payment_processor")), NEW_STRIPE_ENTRY_ID)


def main():
    update_platform_payment_accounts_json()
    update_organizations()
    update_campaigns()
    logger.info("Migration complete!")


if __name__ == "__main__":
    main()
