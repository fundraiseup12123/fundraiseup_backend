from __future__ import annotations

import logging
from typing import Any
from db import rest_get

logger = logging.getLogger(__name__)


def get_donor_identifier(row: dict[str, Any]) -> str:
    """Returns a normalized unique donor key for grouping/deduplicating attempts."""
    email = str(row.get("email") or "").strip().lower()
    if email:
        return f"email:{email}"
    first_name = str(row.get("first_name") or "").strip().lower()
    last_name = str(row.get("last_name") or "").strip().lower()
    campaign_id = str(row.get("campaign_id") or "").strip()
    if first_name or last_name:
        return f"name:{first_name}:{last_name}:{campaign_id}"
    return f"id:{row.get('id')}"


def filter_and_deduplicate_failed_donations(
    rows: list[dict[str, Any]],
    organization_id: str | None = None,
    campaign_id: str | None = None,
) -> list[dict[str, Any]]:
    """Filters out failed donations from donors who subsequently succeeded,

    and deduplicates multiple failed attempts from the same donor so only the
    latest attempt is returned.
    """
    if not rows:
        return []

    # 1. Collect donor emails and names from the failed rows
    emails_to_check: set[str] = set()
    names_to_check: set[tuple[str, str]] = set()

    for r in rows:
        email = str(r.get("email") or "").strip().lower()
        if email:
            emails_to_check.add(email)
        else:
            fn = str(r.get("first_name") or "").strip().lower()
            ln = str(r.get("last_name") or "").strip().lower()
            if fn or ln:
                names_to_check.add((fn, ln))

    # 2. Check which of these donors have at least one succeeded donation
    succeeded_emails: set[str] = set()
    succeeded_names: set[tuple[str, str]] = set()

    try:
        # Check emails in batches of 50
        email_list = list(emails_to_check)
        for i in range(0, len(email_list), 50):
            batch = email_list[i : i + 50]
            succ_params: dict[str, str] = {
                "status": "eq.succeeded",
                "email": f"in.({','.join(batch)})",
                "select": "email",
                "limit": "5000",
            }
            if organization_id:
                succ_params["organization_id"] = f"eq.{organization_id}"
            if campaign_id:
                succ_params["campaign_id"] = f"eq.{campaign_id}"
            succ_rows = rest_get("donations", params=succ_params) or []
            for s in succ_rows:
                em = str(s.get("email") or "").strip().lower()
                if em:
                    succeeded_emails.add(em)

        # For rows without email, check names if present
        if names_to_check:
            name_params: dict[str, str] = {
                "status": "eq.succeeded",
                "select": "first_name,last_name",
                "limit": "5000",
            }
            if organization_id:
                name_params["organization_id"] = f"eq.{organization_id}"
            if campaign_id:
                name_params["campaign_id"] = f"eq.{campaign_id}"
            succ_name_rows = rest_get("donations", params=name_params) or []
            for s in succ_name_rows:
                s_fn = str(s.get("first_name") or "").strip().lower()
                s_ln = str(s.get("last_name") or "").strip().lower()
                if (s_fn, s_ln) in names_to_check:
                    succeeded_names.add((s_fn, s_ln))
    except Exception as exc:
        logger.exception("Error checking succeeded donations for failed filter: %s", exc)

    # 3. Deduplicate: remove donors who succeeded, and keep only latest failed attempt
    # Assumes rows are already sorted by created_at desc (standard for donation listings)
    seen_donor_keys: set[str] = set()
    filtered: list[dict[str, Any]] = []

    for r in rows:
        email = str(r.get("email") or "").strip().lower()
        fn = str(r.get("first_name") or "").strip().lower()
        ln = str(r.get("last_name") or "").strip().lower()

        # If this donor has succeeded, exclude them from failed list
        if email and email in succeeded_emails:
            continue
        if not email and (fn or ln) and (fn, ln) in succeeded_names:
            continue

        # Deduplicate multiple failed attempts from the same donor (keep newest only)
        key = get_donor_identifier(r)
        if key in seen_donor_keys:
            continue
        seen_donor_keys.add(key)
        filtered.append(r)

    return filtered
