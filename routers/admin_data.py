from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from auth import AuthUser, require_auth, require_org_access
from currency import convert_to_reporting, estimate_processing_fee
from db import rest_get, rest_get_one

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
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="member")
    select_cols = (
        "id,first_name,last_name,email,amount,currency,frequency,status,payment_method,"
        "honoree_name,created_at,campaign_id,platform_fee,processing_fee,payout_amount,organization_id"
    )
    params: dict[str, str] = {
        "organization_id": f"eq.{org_id}",
        "select": select_cols,
        "order": "created_at.desc",
        "limit": str(limit + 1),
        "offset": str(offset),
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
    if offset == 0:
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
                "limit": str(limit),
            }
            if campaign_id:
                orphan_params["campaign_id"] = f"eq.{campaign_id}"
            if status:
                orphan_params["status"] = f"eq.{status}"
            if frequency and frequency in {"once", "monthly"}:
                orphan_params["frequency"] = f"eq.{frequency}"
            orphans = rest_get("donations", params=orphan_params)
            if orphans:
                seen = {str(r.get("id")) for r in rows}
                for row in orphans:
                    row_id = str(row.get("id") or "")
                    if row_id and row_id not in seen:
                        rows.append(row)
                        seen.add(row_id)
                rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)

    has_more = len(rows) > limit
    total_amount = sum(float(r.get("amount", 0)) for r in rows[:limit])
    return {"donations": rows[:limit], "has_more": has_more, "total_amount": total_amount}


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
    params: dict[str, str] = {
        "organization_id": f"eq.{org_id}",
        "status": "eq.succeeded",
        "select": "amount,currency,frequency,created_at,campaign_id,payment_method,honoree_name,comment,utm",
        "order": "created_at.desc",
        "limit": "1000",
    }

    if campaign_id:
        params["campaign_id"] = f"eq.{campaign_id}"
    elif designation:
        matching = [c["id"] for c in campaigns if c.get("designation") == designation]
        if not matching:
            return _empty_insights(reporting_currency, date_preset, campaigns)
        params["campaign_id"] = f"in.({','.join(matching)})"

    if utm_source:
        params["utm->>source"] = f"eq.{utm_source}"
    if frequency:
        params["frequency"] = f"eq.{frequency}"

    date_from, date_to = _date_range(date_preset)
    if date_from and date_to:
        params["and"] = f"(created_at.gte.{date_from},created_at.lte.{date_to})"
    elif date_from:
        params["created_at"] = f"gte.{date_from}"
    elif date_to:
        params["created_at"] = f"lte.{date_to}"

    rows = rest_get("donations", params=params)
    rows = _merge_orphan_donations(
        org_id,
        rows,
        campaigns,
        campaign_id=campaign_id,
        designation=designation,
        status="succeeded",
        frequency=frequency,
        date_from=date_from,
        date_to=date_to,
        select=params["select"],
    )

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
