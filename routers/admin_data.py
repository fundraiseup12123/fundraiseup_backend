from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import AuthUser, require_auth, require_org_access
from currency import convert_to_reporting, estimate_processing_fee
from db import rest_get, rest_get_one, rest_insert, rest_patch
from email_templates import (
    EDITABLE_TEMPLATE_KEYS,
    TEMPLATE_TOKENS_HELP,
    default_editable_template,
)
from pydantic import BaseModel, Field

router = APIRouter(prefix="/admin", tags=["admin-data"])


@router.get("/orgs/{org_id}/donations")
def admin_list_donations(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    campaign_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
    frequency: str | None = Query(None),
    date_preset: str | None = Query(None),
    sort: str = Query("date_desc", pattern="^(date_desc|asc|desc)$"),
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="member")
    org = rest_get_one(
        "organizations",
        params={"id": f"eq.{org_id}", "select": "reporting_currency"},
    )
    reporting_currency = str((org or {}).get("reporting_currency") or "USD").upper()
    amount_sort = sort in {"asc", "desc"}
    sort_desc = sort == "desc"
    select_cols = (
        "id,first_name,last_name,email,amount,currency,frequency,status,payment_method,"
        "honoree_name,created_at,campaign_id,platform_fee,processing_fee,payout_amount,"
        "organization_id,crypto_amount,crypto_currency"
    )
    # Amount sort loads the filtered set so FX conversion ranks correctly across currencies.
    fetch_limit = 1000 if amount_sort else limit + 1
    params: dict[str, str] = {
        "organization_id": f"eq.{org_id}",
        "select": select_cols,
        "order": "created_at.desc",
        "limit": str(fetch_limit),
        "offset": str(0 if amount_sort else offset),
    }
    if campaign_id:
        params["campaign_id"] = f"eq.{campaign_id}"
    if status:
        params["status"] = f"eq.{status}"
    if frequency and frequency in {"once", "monthly"}:
        params["frequency"] = f"eq.{frequency}"
    if date_preset and date_preset != "all":
        date_from, date_to = _date_range(date_preset)
        if date_from and date_to:
            params["and"] = f"(created_at.gte.{date_from},created_at.lte.{date_to})"
        elif date_from:
            params["created_at"] = f"gte.{date_from}"
        elif date_to:
            params["created_at"] = f"lte.{date_to}"
    rows = rest_get("donations", params=params)

    # Include older PayPal rows that were saved without organization_id but belong to this org's campaigns.
    if offset == 0 or amount_sort:
        org_campaigns = rest_get(
            "campaigns",
            params={"organization_id": f"eq.{org_id}", "select": "id", "limit": "200"},
        )
        campaign_ids = [str(c["id"]) for c in org_campaigns if c.get("id")]
        if campaign_ids:
            orphan_params: dict[str, str] = {
                "organization_id": "is.null",
                "campaign_id": f"in.({','.join(campaign_ids)})",
                "select": select_cols,
                "order": "created_at.desc",
                "limit": str(fetch_limit),
            }
            if campaign_id:
                orphan_params["campaign_id"] = f"eq.{campaign_id}"
            if status:
                orphan_params["status"] = f"eq.{status}"
            if frequency and frequency in {"once", "monthly"}:
                orphan_params["frequency"] = f"eq.{frequency}"
            if date_preset and date_preset != "all":
                date_from, date_to = _date_range(date_preset)
                if date_from and date_to:
                    orphan_params["and"] = f"(created_at.gte.{date_from},created_at.lte.{date_to})"
                elif date_from:
                    orphan_params["created_at"] = f"gte.{date_from}"
                elif date_to:
                    orphan_params["created_at"] = f"lte.{date_to}"
            orphans = rest_get("donations", params=orphan_params)
            if orphans:
                seen = {str(r.get("id")) for r in rows}
                for row in orphans:
                    row_id = str(row.get("id") or "")
                    if row_id and row_id not in seen:
                        rows.append(row)
                        seen.add(row_id)

    if amount_sort:
        # Stable: newest first within equal converted amounts.
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        rows.sort(key=lambda r: _row_amount(r, reporting_currency), reverse=sort_desc)
        page = rows[offset : offset + limit]
        has_more = len(rows) > offset + limit
    else:
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        has_more = len(rows) > limit
        page = rows[:limit]

    total_amount = sum(_row_amount(r, reporting_currency) for r in page)
    for row in page:
        original_currency = str(row.get("currency") or "USD").upper()
        row["original_amount"] = float(row.get("amount") or 0)
        row["original_currency"] = original_currency
        row["reporting_amount"] = _row_amount(row, reporting_currency)
        row["reporting_currency"] = reporting_currency

    _attach_last_emails(page)

    return {
        "donations": page,
        "has_more": has_more,
        "total_amount": total_amount,
        "reporting_currency": reporting_currency,
    }


def _attach_last_emails(rows: list[dict[str, Any]]) -> None:
    ids = [str(r.get("id")) for r in rows if r.get("id")]
    if not ids:
        return
    logs = rest_get(
        "email_logs",
        params={
            "donation_id": f"in.({','.join(ids)})",
            "select": "donation_id,template_key,sent_at",
            "order": "sent_at.desc",
            "limit": str(max(50, len(ids) * 8)),
        },
    )
    latest: dict[str, dict[str, Any]] = {}
    for log in logs:
        donation_id = str(log.get("donation_id") or "")
        if donation_id and donation_id not in latest:
            latest[donation_id] = log
    for row in rows:
        info = latest.get(str(row.get("id") or ""))
        row["last_email_template_key"] = (info or {}).get("template_key")
        row["last_email_sent_at"] = (info or {}).get("sent_at")


class EmailTemplateUpdate(BaseModel):
    subject: str = Field(min_length=1, max_length=500)
    headline: str = Field(min_length=1, max_length=500)
    body_html: str = ""
    banner_url: str | None = None
    logo_url: str | None = None
    cta_label: str | None = None


@router.get("/orgs/{org_id}/email-templates")
def list_email_templates(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    saved = rest_get(
        "email_templates",
        params={
            "organization_id": f"eq.{org_id}",
            "select": "template_key,subject,headline,body_html,banner_url,logo_url,cta_label,updated_at",
        },
    )
    by_key = {str(row.get("template_key")): row for row in saved if row.get("template_key")}
    templates: list[dict[str, Any]] = []
    for key in EDITABLE_TEMPLATE_KEYS:
        defaults = default_editable_template(key)
        row = by_key.get(key)
        if row:
            templates.append(
                {
                    **defaults,
                    **{k: row.get(k) for k in ("subject", "headline", "body_html", "banner_url", "logo_url", "cta_label")},
                    "template_key": key,
                    "updated_at": row.get("updated_at"),
                    "is_custom": True,
                }
            )
        else:
            templates.append(defaults)
    return {"templates": templates, "tokens_help": TEMPLATE_TOKENS_HELP}


@router.get("/orgs/{org_id}/email-templates/{template_key}")
def get_email_template(
    org_id: str,
    template_key: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    if template_key not in EDITABLE_TEMPLATE_KEYS:
        raise HTTPException(status_code=404, detail="Unknown template")
    defaults = default_editable_template(template_key)
    row = rest_get_one(
        "email_templates",
        params={
            "organization_id": f"eq.{org_id}",
            "template_key": f"eq.{template_key}",
            "select": "template_key,subject,headline,body_html,banner_url,logo_url,cta_label,updated_at",
        },
    )
    if not row:
        return defaults
    return {
        **defaults,
        **{k: row.get(k) for k in ("subject", "headline", "body_html", "banner_url", "logo_url", "cta_label")},
        "template_key": template_key,
        "updated_at": row.get("updated_at"),
        "is_custom": True,
    }


@router.patch("/orgs/{org_id}/email-templates/{template_key}")
def update_email_template(
    org_id: str,
    template_key: str,
    payload: EmailTemplateUpdate,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    if template_key not in EDITABLE_TEMPLATE_KEYS:
        raise HTTPException(status_code=404, detail="Unknown template")

    now = datetime.now(timezone.utc).isoformat()
    row = {
        "organization_id": org_id,
        "template_key": template_key,
        "subject": payload.subject.strip(),
        "headline": payload.headline.strip(),
        "body_html": payload.body_html or "",
        "banner_url": (payload.banner_url or "").strip() or None,
        "logo_url": (payload.logo_url or "").strip() or None,
        "cta_label": (payload.cta_label or "").strip() or None,
        "updated_at": now,
    }
    existing = rest_get_one(
        "email_templates",
        params={
            "organization_id": f"eq.{org_id}",
            "template_key": f"eq.{template_key}",
            "select": "id",
        },
    )
    if existing and existing.get("id"):
        saved = rest_patch("email_templates", row, match={"id": str(existing["id"])})
    else:
        saved = rest_insert("email_templates", row)

    if not saved:
        raise HTTPException(
            status_code=503,
            detail="Could not save template. Run backend/sql/011_email_templates.sql on Supabase.",
        )

    defaults = default_editable_template(template_key)
    return {
        **defaults,
        **{k: row.get(k) for k in ("subject", "headline", "body_html", "banner_url", "logo_url", "cta_label")},
        "template_key": template_key,
        "updated_at": now,
        "is_custom": True,
    }


def _merge_orphan_donations(
    org_id: str,
    rows: list[dict[str, Any]],
    campaigns: list[dict[str, Any]],
    *,
    campaign_id: str | None = None,
    designation: str | None = None,
    status: str | None = None,
    frequency: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    select: str,
) -> list[dict[str, Any]]:
    """Include PayPal rows saved without organization_id but tied to this org's campaigns."""
    campaign_ids = [str(c["id"]) for c in campaigns if c.get("id")]
    if not campaign_ids:
        return rows

    orphan_params: dict[str, str] = {
        "organization_id": "is.null",
        "campaign_id": f"in.({','.join(campaign_ids)})",
        "select": select,
        "order": "created_at.desc",
        "limit": "1000",
    }
    if campaign_id:
        orphan_params["campaign_id"] = f"eq.{campaign_id}"
    elif designation:
        matching = [str(c["id"]) for c in campaigns if c.get("designation") == designation]
        if not matching:
            return rows
        orphan_params["campaign_id"] = f"in.({','.join(matching)})"
    if status:
        orphan_params["status"] = f"eq.{status}"
    if frequency and frequency in {"once", "monthly"}:
        orphan_params["frequency"] = f"eq.{frequency}"
    if date_from and date_to:
        orphan_params["and"] = f"(created_at.gte.{date_from},created_at.lte.{date_to})"
    elif date_from:
        orphan_params["created_at"] = f"gte.{date_from}"
    elif date_to:
        orphan_params["created_at"] = f"lte.{date_to}"

    orphans = rest_get("donations", params=orphan_params)
    if not orphans:
        return rows

    seen = {str(r.get("id")) for r in rows}
    merged = list(rows)
    for row in orphans:
        row_id = str(row.get("id") or "")
        if row_id and row_id not in seen:
            merged.append(row)
            seen.add(row_id)
    merged.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return merged


@router.get("/orgs/{org_id}/donations/{donation_id}")
def admin_donation_detail(
    org_id: str,
    donation_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="member")
    donation = rest_get_one(
        "donations",
        params={"id": f"eq.{donation_id}", "organization_id": f"eq.{org_id}", "select": "*"},
    )
    if not donation:
        donation = rest_get_one(
            "donations",
            params={"id": f"eq.{donation_id}", "select": "*"},
        )
        if donation and donation.get("campaign_id"):
            campaign_row = rest_get_one(
                "campaigns",
                params={"id": f"eq.{donation['campaign_id']}", "select": "organization_id"},
            )
            if not campaign_row or str(campaign_row.get("organization_id")) != org_id:
                donation = None
    if not donation:
        raise HTTPException(status_code=404, detail="Donation not found")

    donation = _enrich_donation_fees(donation)

    campaign = None
    if donation.get("campaign_id"):
        campaign = rest_get_one(
            "campaigns",
            params={
                "id": f"eq.{donation['campaign_id']}",
                "select": "id,name,slug,designation",
            },
        )

    emails = rest_get(
        "email_logs",
        params={"donation_id": f"eq.{donation_id}", "select": "*", "order": "sent_at.desc"},
    )
    return {"donation": donation, "campaign": campaign, "emails": emails}


def _insights_countable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match donations list: include everything except explicitly failed/refunded rows."""
    excluded = {"failed", "canceled", "cancelled", "refunded", "disputed"}
    return [r for r in rows if str(r.get("status") or "").lower() not in excluded]


def _admin_org_donation_rows(
    org_id: str,
    *,
    select: str,
    campaigns: list[dict[str, Any]],
    campaign_id: str | None = None,
    designation: str | None = None,
    frequency: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Shared org donation query used by insights (same inclusion rules as donations list)."""
    params: dict[str, str] = {
        "organization_id": f"eq.{org_id}",
        "select": select,
        "order": "created_at.desc",
        "limit": str(limit),
    }

    if campaign_id:
        params["campaign_id"] = f"eq.{campaign_id}"
    elif designation:
        matching = [c["id"] for c in campaigns if c.get("designation") == designation]
        if not matching:
            return []
        params["campaign_id"] = f"in.({','.join(matching)})"

    if frequency and frequency in {"once", "monthly"}:
        params["frequency"] = f"eq.{frequency}"

    if date_from and date_to:
        params["and"] = f"(created_at.gte.{date_from},created_at.lte.{date_to})"
    elif date_from:
        params["created_at"] = f"gte.{date_from}"
    elif date_to:
        params["created_at"] = f"lte.{date_to}"

    rows = rest_get("donations", params=params)
    return _merge_orphan_donations(
        org_id,
        rows,
        campaigns,
        campaign_id=campaign_id,
        designation=designation,
        status=None,
        frequency=frequency,
        date_from=date_from,
        date_to=date_to,
        select=select,
    )


@router.get("/orgs/{org_id}/insights")
def admin_insights(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    campaign_id: str | None = Query(None),
    designation: str | None = Query(None),
    utm_source: str | None = Query(None),
    frequency: str | None = Query(None),
    date_preset: str = Query("all"),
    interval: str = Query("hourly"),
    reporting_currency: str = Query("GBP"),
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="member")

    org = rest_get_one(
        "organizations",
        params={"id": f"eq.{org_id}", "select": "reporting_currency"},
    )
    reporting_currency = (org or {}).get("reporting_currency") or reporting_currency or "USD"

    campaigns = rest_get(
        "campaigns",
        params={"organization_id": f"eq.{org_id}", "select": "id,name,designation"},
    )
    date_from, date_to = _date_range(date_preset)

    if designation and not campaign_id:
        matching = [c["id"] for c in campaigns if c.get("designation") == designation]
        if not matching:
            return _empty_insights(reporting_currency, date_preset, campaigns)

    rows = _admin_org_donation_rows(
        org_id,
        select="amount,currency,frequency,created_at,campaign_id,payment_method,honoree_name,comment,utm,status",
        campaigns=campaigns,
        campaign_id=campaign_id,
        designation=designation,
        frequency=frequency,
        date_from=date_from,
        date_to=date_to,
    )

    if utm_source:
        rows = [
            r
            for r in rows
            if isinstance(r.get("utm"), dict) and r["utm"].get("source") == utm_source
        ]

    rows = _insights_countable(rows)

    recurring = [r for r in rows if r.get("frequency") == "monthly"]
    one_time = [r for r in rows if r.get("frequency") != "monthly"]
    total_raised = sum(_row_amount(r, reporting_currency) for r in rows)
    first_installments = sum(_row_amount(r, reporting_currency) for r in recurring)
    one_time_total = sum(_row_amount(r, reporting_currency) for r in one_time)

    chart = _build_chart(rows, interval, reporting_currency)
    payment_methods = _breakdown(
        rows,
        lambda r: (r.get("payment_method") or "card").replace("_", " "),
        reporting_currency,
    )
    campaign_name_by_id = {c["id"]: c.get("name", "Unknown") for c in campaigns}
    campaign_breakdown = _breakdown(
        rows,
        lambda r: campaign_name_by_id.get(r.get("campaign_id"), "Unknown"),
        reporting_currency,
    )
    hour_breakdown = _breakdown(
        rows,
        lambda r: _hour_label(r.get("created_at", "")),
        reporting_currency,
    )

    sources = sorted(
        {
            (r.get("utm") or {}).get("source")
            for r in rest_get(
                "donations",
                params={
                    "organization_id": f"eq.{org_id}",
                    "select": "utm",
                    "limit": "500",
                },
            )
            if isinstance(r.get("utm"), dict) and r["utm"].get("source")
        }
    )

    return {
        "reporting_currency": reporting_currency.upper(),
        "date_label": _date_label(date_preset),
        "raised": {"total": round(total_raised, 2), "count": len(rows)},
        "first_installments": {"total": round(first_installments, 2), "count": len(recurring)},
        "one_time": {"total": round(one_time_total, 2), "count": len(one_time)},
        "chart": chart,
        "first_installments_chart": _build_chart(recurring, interval, reporting_currency),
        "one_time_chart": _build_chart(one_time, interval, reporting_currency),
        "avg_donation": round(total_raised / len(rows), 2) if rows else 0,
        "retention_rate": round((len(recurring) / len(rows)) * 100, 1) if rows else 0,
        "payment_methods": payment_methods,
        "tribute_count": sum(1 for r in rows if r.get("honoree_name")),
        "comment_count": sum(1 for r in rows if r.get("comment")),
        "campaign_breakdown": campaign_breakdown,
        "hour_breakdown": hour_breakdown,
        "filter_options": {
            "campaigns": campaigns,
            "designations": sorted({c.get("designation") for c in campaigns if c.get("designation")}),
            "sources": sources,
        },
    }


def _empty_insights(reporting_currency: str, date_preset: str, campaigns: list[dict[str, Any]]) -> dict[str, Any]:
    empty_chart = [{"hour": h, "amount": 0, "count": 0, "label": _hour_label_from_int(h)} for h in range(24)]
    return {
        "reporting_currency": reporting_currency.upper(),
        "date_label": _date_label(date_preset),
        "raised": {"total": 0, "count": 0},
        "first_installments": {"total": 0, "count": 0},
        "one_time": {"total": 0, "count": 0},
        "chart": empty_chart,
        "first_installments_chart": empty_chart,
        "one_time_chart": empty_chart,
        "avg_donation": 0,
        "retention_rate": 0,
        "payment_methods": [],
        "tribute_count": 0,
        "comment_count": 0,
        "campaign_breakdown": [],
        "hour_breakdown": [],
        "filter_options": {
            "campaigns": campaigns,
            "designations": sorted({c.get("designation") for c in campaigns if c.get("designation")}),
            "sources": [],
        },
    }


def _date_range(preset: str) -> tuple[str | None, str | None]:
    now = datetime.now(timezone.utc)
    start = lambda d: d.replace(hour=0, minute=0, second=0, microsecond=0)
    end = lambda d: d.replace(hour=23, minute=59, second=59, microsecond=999000)

    if preset == "today":
        return start(now).isoformat(), end(now).isoformat()
    if preset == "yesterday":
        day = now - timedelta(days=1)
        return start(day).isoformat(), end(day).isoformat()
    if preset == "7d":
        return start(now - timedelta(days=6)).isoformat(), end(now).isoformat()
    if preset == "30d":
        return start(now - timedelta(days=29)).isoformat(), end(now).isoformat()
    if preset == "month":
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return month_start.isoformat(), end(now).isoformat()
    return None, None


def _date_label(preset: str) -> str:
    now = datetime.now(timezone.utc)
    fmt = "%b %d, %Y"
    if preset == "today":
        return now.strftime(fmt)
    if preset == "yesterday":
        return (now - timedelta(days=1)).strftime(fmt)
    if preset == "7d":
        return f"{(now - timedelta(days=6)).strftime(fmt)} – {now.strftime(fmt)}"
    if preset == "30d":
        return f"{(now - timedelta(days=29)).strftime(fmt)} – {now.strftime(fmt)}"
    if preset == "month":
        return f"{now.replace(day=1).strftime(fmt)} – {now.strftime(fmt)}"
    return "All time"


def _hour_label(created_at: str) -> str:
    try:
        hour = int(created_at[11:13])
    except (ValueError, IndexError):
        hour = 0
    return _hour_label_from_int(hour)


def _hour_label_from_int(hour: int) -> str:
    suffix = "PM" if hour >= 12 else "AM"
    h = hour % 12 or 12
    return f"{h} {suffix}"


def _enrich_donation_fees(donation: dict[str, Any]) -> dict[str, Any]:
    """Fill missing fee fields for older donation rows."""
    amount = float(donation.get("amount", 0))
    currency = str(donation.get("currency", "USD")).upper()
    base_amount = donation.get("base_amount")
    base = float(base_amount) if base_amount is not None else amount
    fee_covered = donation.get("fee_covered") in (True, "true", "True", 1)

    if donation.get("processing_fee") is None:
        if fee_covered:
            if base_amount is not None:
                donation["processing_fee"] = max(0.0, round(amount - base, 2))
            else:
                donation["base_amount"] = amount
                donation["processing_fee"] = estimate_processing_fee(amount, currency)
        else:
            donation["base_amount"] = base
            donation["processing_fee"] = estimate_processing_fee(base, currency)

    if donation.get("base_amount") is None:
        donation["base_amount"] = base if not fee_covered else float(donation.get("base_amount") or amount)

    if donation.get("payout_amount") is None:
        processing = float(donation.get("processing_fee") or 0)
        gift_base = float(donation.get("base_amount") or amount)
        donation["payout_amount"] = gift_base if fee_covered else max(0.0, round(amount - processing, 2))

    if donation.get("platform_fee") is None:
        donation["platform_fee"] = 0

    return donation


def _row_amount(row: dict[str, Any], reporting_currency: str) -> float:
    return convert_to_reporting(
        float(row.get("amount", 0)),
        str(row.get("currency", "USD")),
        reporting_currency,
    )


def _build_chart(rows: list[dict[str, Any]], interval: str, reporting_currency: str) -> list[dict[str, Any]]:
    if interval == "daily":
        buckets: dict[str, dict[str, float | int]] = {}
        for row in rows:
            created = row.get("created_at", "")
            if not created:
                continue
            key = created[:10]
            bucket = buckets.setdefault(key, {"amount": 0.0, "count": 0})
            bucket["amount"] = float(bucket["amount"]) + _row_amount(row, reporting_currency)
            bucket["count"] = int(bucket["count"]) + 1
        keys = sorted(buckets.keys())
        if not keys:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            return [{"hour": 0, "amount": 0, "count": 0, "label": today}]
        return [
            {
                "hour": index,
                "amount": round(float(buckets[key]["amount"]), 2),
                "count": int(buckets[key]["count"]),
                "label": datetime.fromisoformat(f"{key}T00:00:00+00:00").strftime("%b %d"),
            }
            for index, key in enumerate(keys)
        ]

    buckets: dict[int, dict[str, float | int]] = {}
    for row in rows:
        created = row.get("created_at", "")
        if not created:
            continue
        try:
            hour = int(created[11:13])
        except (ValueError, IndexError):
            hour = 0
        bucket = buckets.setdefault(hour, {"amount": 0.0, "count": 0})
        bucket["amount"] = float(bucket["amount"]) + _row_amount(row, reporting_currency)
        bucket["count"] = int(bucket["count"]) + 1
    return [
        {
            "hour": hour,
            "amount": round(float(buckets.get(hour, {}).get("amount", 0)), 2),
            "count": int(buckets.get(hour, {}).get("count", 0)),
            "label": _hour_label_from_int(hour),
        }
        for hour in range(24)
    ]


def _breakdown(rows: list[dict[str, Any]], key_fn, reporting_currency: str) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, float | int]] = {}
    for row in rows:
        key = key_fn(row) or "Unknown"
        bucket = totals.setdefault(key, {"count": 0, "total": 0.0})
        bucket["count"] = int(bucket["count"]) + 1
        bucket["total"] = float(bucket["total"]) + _row_amount(row, reporting_currency)
    return [
        {"label": label, "count": int(values["count"]), "total": round(float(values["total"]), 2)}
        for label, values in sorted(totals.items(), key=lambda item: float(item[1]["total"]), reverse=True)
    ]


def _build_hourly_chart(rows: list[dict[str, Any]], reporting_currency: str = "USD") -> list[dict[str, Any]]:
    return _build_chart(rows, "hourly", reporting_currency)


@router.get("/orgs/{org_id}/supporters")
def list_supporters(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    require_org_access(org_id, user, min_role="member")
    rows = rest_get(
        "donations",
        params={
            "organization_id": f"eq.{org_id}",
            "status": "eq.succeeded",
            "select": "id,first_name,last_name,email,amount",
            "order": "created_at.desc",
            "limit": "2000",
        },
    )

    aggregated: dict[str, dict[str, Any]] = {}
    for row in rows:
        email = (row.get("email") or "").strip().lower()
        key = email or f"{row.get('first_name', '')}_{row.get('last_name', '')}".strip().lower()
        if not key:
            continue

        bucket = aggregated.setdefault(
            key,
            {
                "id": key,
                "first_name": row.get("first_name"),
                "last_name": row.get("last_name"),
                "email": email or None,
                "total_donated": 0.0,
                "donation_count": 0,
            },
        )
        bucket["total_donated"] = float(bucket["total_donated"]) + float(row.get("amount") or 0)
        bucket["donation_count"] = int(bucket["donation_count"]) + 1

    supporters = sorted(
        aggregated.values(),
        key=lambda item: float(item["total_donated"]),
        reverse=True,
    )
    return supporters[:limit]


@router.get("/orgs/{org_id}/questions/{campaign_id}")
def list_questions(
    org_id: str,
    campaign_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> list[dict[str, Any]]:
    require_org_access(org_id, user, min_role="member")
    return rest_get(
        "questions",
        params={"campaign_id": f"eq.{campaign_id}", "select": "*", "order": "sort_order.asc"},
    )


@router.get("/orgs/{org_id}/tributes")
def list_tributes(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    campaign_id: str | None = Query(None),
) -> list[dict[str, Any]]:
    require_org_access(org_id, user, min_role="member")
    params: dict[str, str] = {
        "organization_id": f"eq.{org_id}",
        "honoree_name": "not.is.null",
        "select": "id,honoree_name,comment,created_at,campaign_id,first_name,last_name",
        "order": "created_at.desc",
        "limit": "100",
    }
    if campaign_id:
        params["campaign_id"] = f"eq.{campaign_id}"
    rows = rest_get("donations", params=params)
    return [
        {
            "id": row["id"],
            "honoree_name": row.get("honoree_name"),
            "message": row.get("comment"),
            "created_at": row.get("created_at"),
            "campaign_id": row.get("campaign_id"),
            "donor_name": f"{row.get('first_name', '')} {row.get('last_name', '')}".strip() or None,
        }
        for row in rows
        if row.get("honoree_name")
    ]


@router.get("/orgs/{org_id}/emails")
def list_email_logs(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    require_org_access(org_id, user, min_role="member")
    rows = rest_get(
        "email_logs",
        params={
            "organization_id": f"eq.{org_id}",
            "select": "id,recipient_email,subject,template_key,sent_at,opened_at,donation_id",
            "order": "sent_at.desc",
            "limit": str(limit),
        },
    )
    if rows:
        return rows

    donations = rest_get(
        "donations",
        params={"organization_id": f"eq.{org_id}", "select": "id", "limit": "500"},
    )
    donation_ids = [d["id"] for d in donations]
    if not donation_ids:
        return []
    ids_filter = ",".join(donation_ids)
    return rest_get(
        "email_logs",
        params={
            "donation_id": f"in.({ids_filter})",
            "select": "id,recipient_email,subject,template_key,sent_at,opened_at,donation_id",
            "order": "sent_at.desc",
            "limit": str(limit),
        },
    )


@router.get("/orgs/{org_id}/recurring")
def list_recurring_donations(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    limit: int = Query(50, ge=1, le=100),
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="member")
    rows = rest_get(
        "donations",
        params={
            "organization_id": f"eq.{org_id}",
            "frequency": "eq.monthly",
            "select": "id,first_name,last_name,email,amount,currency,status,created_at,campaign_id",
            "order": "created_at.desc",
            "limit": str(limit),
        },
    )
    total = sum(float(r.get("amount", 0)) for r in rows)
    return {"donations": rows, "total_amount": total, "count": len(rows)}


@router.get("/orgs/{org_id}/exports/donations.csv")
def export_donations_csv(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    campaign_id: str | None = Query(None),
) -> dict[str, str]:
    require_org_access(org_id, user, min_role="member")
    data = admin_list_donations(org_id, user, campaign_id=campaign_id, limit=1000, offset=0)
    lines = ["id,first_name,last_name,email,amount,currency,frequency,status,created_at"]
    for d in data["donations"]:
        lines.append(
            f"{d.get('id')},{d.get('first_name')},{d.get('last_name')},{d.get('email','')},{d.get('amount')},{d.get('currency')},{d.get('frequency')},{d.get('status')},{d.get('created_at')}"
        )
    return {"csv": "\n".join(lines)}


def _ga4_date_range(preset: str, date_from: str | None, date_to: str | None) -> tuple[str, str]:
    """Return GA4 date strings.

    Prefer relative dates (today / NdagsAgo) so ranges follow the GA4 property
    timezone — absolute UTC dates often make "Today" look empty for users
    outside UTC.
    """
    if date_from and date_to:
        return date_from[:10], date_to[:10]
    if preset == "today":
        return "today", "today"
    if preset == "yesterday":
        return "yesterday", "yesterday"
    if preset == "3d":
        return "2daysAgo", "today"
    if preset == "7d":
        return "6daysAgo", "today"
    if preset == "30d":
        return "29daysAgo", "today"
    if preset == "90d":
        return "89daysAgo", "today"
    if preset == "month":
        # First day of current month in property TZ isn't available as a relative
        # token — use UTC calendar month as a close fallback.
        now = datetime.now(timezone.utc).date()
        return now.replace(day=1).isoformat(), "today"
    # default ~28 days
    return "27daysAgo", "today"


@router.get("/orgs/{org_id}/google-analytics")
def admin_google_analytics(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    date_preset: str = Query("30d"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    campaign_id: str | None = Query(None),
    property_id: str | None = Query(None),
    bootstrap: bool = Query(False),
) -> dict[str, Any]:
    """Fetch GA4 reports scoped to one campaign Property ID (never unscoped 'all')."""
    require_org_access(org_id, user, min_role="member")

    # campaigns.name (not title) — wrong columns make PostgREST return []
    campaigns = rest_get(
        "campaigns",
        params={
            "organization_id": f"eq.{org_id}",
            "select": "id,name,slug",
            "order": "created_at.desc",
            "limit": "200",
        },
    )
    campaign_ids = [str(c["id"]) for c in campaigns if c.get("id")]
    content_by_id: dict[str, dict[str, Any]] = {}
    if campaign_ids:
        id_list = ",".join(campaign_ids)
        contents = rest_get(
            "campaign_content",
            params={
                "campaign_id": f"in.({id_list})",
                "select": "campaign_id,ga4_measurement_id,gtm_container_id,ga4_property_id",
                "limit": "200",
            },
        )
        # Migration 020 not applied yet → column missing → empty list; retry without it
        if not contents:
            contents = rest_get(
                "campaign_content",
                params={
                    "campaign_id": f"in.({id_list})",
                    "select": "campaign_id,ga4_measurement_id,gtm_container_id",
                    "limit": "200",
                },
            )
        content_by_id = {
            str(row.get("campaign_id")): row for row in contents if row.get("campaign_id")
        }

    campaign_options = []
    for campaign in campaigns:
        cid = str(campaign.get("id") or "")
        content = content_by_id.get(cid) or {}
        campaign_options.append(
            {
                "id": cid,
                "title": campaign.get("name") or campaign.get("title") or "Campaign",
                "slug": campaign.get("slug") or "",
                "ga4_measurement_id": content.get("ga4_measurement_id") or "",
                "gtm_container_id": content.get("gtm_container_id") or "",
                "ga4_property_id": str(content.get("ga4_property_id") or "").strip(),
            }
        )

    from ga4_client import fetch_dashboard, fetch_realtime_snapshot, ga4_configured, get_property_id

    configured = ga4_configured()
    start, end = _ga4_date_range(date_preset, date_from, date_to)

    # Always lock to one campaign — never return unscoped whole-property data first.
    with_prop = [c for c in campaign_options if c.get("ga4_property_id")]
    selected_campaign = None
    if campaign_id:
        selected_campaign = next((c for c in campaign_options if c["id"] == campaign_id), None)
    if not selected_campaign and with_prop:
        selected_campaign = with_prop[0]
    if not selected_campaign and campaign_options:
        selected_campaign = campaign_options[0]

    resolved_property_id = (property_id or "").strip().replace("properties/", "") or None
    if selected_campaign and selected_campaign.get("ga4_property_id"):
        resolved_property_id = str(selected_campaign["ga4_property_id"])
    if not resolved_property_id:
        resolved_property_id = get_property_id()

    selected_id = str(selected_campaign["id"]) if selected_campaign else ""
    empty_report = {
        "totals": {},
        "timeseries": [],
        "top_pages": [],
        "sources": [],
        "devices": [],
        "countries": [],
        "events": [],
        "error": None,
    }

    payload: dict[str, Any] = {
        "configured": False,
        "service_account_ready": configured,
        "property_id": resolved_property_id or "",
        "date_preset": date_preset,
        "date_from": start,
        "date_to": end,
        "campaigns": campaign_options,
        "selected_campaign_id": selected_id,
        "setup": {
            "needs_service_account": not configured,
            "needs_property_id": configured and not resolved_property_id,
            "hint": (
                "1) Set GA4_SERVICE_ACCOUNT_JSON (or GA4_SERVICE_ACCOUNT_FILE) on the backend and "
                "grant that service account Viewer on each GA4 property. "
                "2) On each campaign Content tab, save GA4 Measurement ID (G-…) for tracking and "
                "GA4 Property ID (numbers only) for this reporting page."
            ),
        },
        **empty_report,
    }

    # Campaign list only — frontend picks campaign_id before fetching charts
    if bootstrap:
        return payload

    can_fetch = bool(configured and resolved_property_id and selected_id)
    payload["configured"] = can_fetch
    if not can_fetch:
        return payload

    # Scope = campaign Property ID only (no pagePath slug filter — that undercounted).
    try:
        report = fetch_dashboard(
            date_from=start,
            date_to=end,
            property_id=resolved_property_id,
            path_contains=None,
        )
        payload.update(report)
        payload["configured"] = True
        payload["property_id"] = resolved_property_id or ""
        payload["selected_campaign_id"] = selected_id
        payload["error"] = None

        # Today often lags in standard reports — attach realtime (~30 min) so visits appear.
        if date_preset == "today":
            payload["realtime"] = fetch_realtime_snapshot(property_id=resolved_property_id)
            totals = payload.get("totals") or {}
            if not float(totals.get("users") or 0) and not float(totals.get("sessions") or 0):
                payload["today_note"] = (
                    "Standard Today report is still empty (GA4 often delays). "
                    "Realtime below shows the last ~30 minutes."
                )
            else:
                payload["today_note"] = None
        else:
            payload["realtime"] = None
            payload["today_note"] = None
    except Exception as exc:
        payload.update({**empty_report, "error": str(exc)})
    return payload
