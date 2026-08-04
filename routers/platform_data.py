"""Platform-wide (super-admin) aggregated donations, insights, and Google Analytics."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import AuthUser, require_super_admin
from currency import convert_to_reporting
from db import rest_get, rest_get_one
from routers import admin_data as ad

router = APIRouter(prefix="/super", tags=["platform-data"])

_GA_PROPERTY_CAP = 15
_DEFAULT_PLATFORM_TZ = "America/Los_Angeles"
_DONATION_SELECT = (
    "id,first_name,last_name,email,amount,currency,frequency,status,payment_method,"
    "honoree_name,created_at,campaign_id,platform_fee,processing_fee,payout_amount,"
    "base_amount,fee_covered,organization_id,crypto_amount,crypto_currency"
)
_INSIGHTS_SELECT = (
    "amount,currency,frequency,created_at,campaign_id,payment_method,"
    "honoree_name,comment,utm,status,device,organization_id"
)


def _org_name_map() -> dict[str, str]:
    rows = rest_get(
        "organizations",
        params={"select": "id,name", "order": "name.asc", "limit": "500"},
    )
    return {str(r["id"]): str(r.get("name") or "Organization") for r in rows if r.get("id")}


def _all_campaigns(organization_id: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, str] = {
        "select": "id,name,slug,designation,organization_id",
        "order": "created_at.desc",
        "limit": "1000",
    }
    if organization_id:
        params["organization_id"] = f"eq.{organization_id}"
    return rest_get("campaigns", params=params)


def _filter_options(
    org_names: dict[str, str],
    campaigns: list[dict[str, Any]],
    *,
    include_sources: bool = False,
) -> dict[str, Any]:
    organizations = [
        {"id": oid, "name": name}
        for oid, name in sorted(org_names.items(), key=lambda item: item[1].lower())
    ]
    campaign_opts = [
        {
            "id": str(c.get("id")),
            "name": c.get("name") or "Campaign",
            "organization_id": str(c.get("organization_id") or ""),
            "organization_name": org_names.get(str(c.get("organization_id") or ""), ""),
            "designation": c.get("designation"),
        }
        for c in campaigns
        if c.get("id")
    ]
    designations = sorted(
        {str(c.get("designation")) for c in campaigns if c.get("designation")}
    )
    sources: list[str] = []
    if include_sources:
        source_rows = rest_get(
            "donations",
            params={"select": "utm", "limit": "2000", "order": "created_at.desc"},
        )
        sources = sorted(
            {
                str((r.get("utm") or {}).get("source"))
                for r in source_rows
                if isinstance(r.get("utm"), dict) and (r.get("utm") or {}).get("source")
            }
        )
    return {
        "organizations": organizations,
        "campaigns": campaign_opts,
        "designations": designations,
        "sources": sources,
    }


@router.get("/donations")
def platform_list_donations(
    user: Annotated[AuthUser, Depends(require_super_admin)],
    campaign_id: str | None = Query(None),
    organization_id: str | None = Query(None),
    limit: int = Query(10000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
    frequency: str | None = Query(None),
    payment_method: str | None = Query(None),
    date_preset: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    sort: str = Query("date_desc", pattern="^(date_desc|asc|desc)$"),
    reporting_currency: str = Query("USD"),
    timezone: str | None = Query(None),
) -> dict[str, Any]:
    reporting_currency = (reporting_currency or "USD").strip().upper() or "USD"
    tz_name = ad._org_zone(timezone or _DEFAULT_PLATFORM_TZ).key
    org_names = _org_name_map()
    campaigns = _all_campaigns(organization_id)
    amount_sort = sort in {"asc", "desc"}
    sort_desc = sort == "desc"

    allowed_methods = {"card", "paypal", "apple_pay", "google_pay", "nowpayments"}
    method_filter = (payment_method or "").strip().lower()
    if method_filter not in allowed_methods:
        method_filter = ""

    fetch_limit = min(10000, max(limit + 1, 5000))
    params: dict[str, str] = {
        "select": _DONATION_SELECT,
        "order": "created_at.desc",
        "limit": str(fetch_limit),
        "offset": "0",
    }
    if organization_id:
        params["organization_id"] = f"eq.{organization_id}"
    if campaign_id:
        params["campaign_id"] = f"eq.{campaign_id}"
    if status:
        params["status"] = f"eq.{status}"
    if frequency and frequency in {"once", "monthly"}:
        params["frequency"] = f"eq.{frequency}"
    if method_filter == "card":
        params["or"] = "(payment_method.eq.card,payment_method.is.null)"
    elif method_filter:
        params["payment_method"] = f"eq.{method_filter}"

    resolved_from: str | None = None
    resolved_to: str | None = None
    if date_preset and date_preset != "all":
        resolved_from, resolved_to = ad._insights_date_range(
            date_preset, date_from, date_to, tz_name
        )
        if resolved_from and resolved_to:
            params["and"] = f"(created_at.gte.{resolved_from},created_at.lte.{resolved_to})"
        elif resolved_from:
            params["created_at"] = f"gte.{resolved_from}"
        elif resolved_to:
            params["created_at"] = f"lte.{resolved_to}"

    rows = rest_get("donations", params=params)

    # When scoped to one org, include orphan PayPal rows for that org's campaigns.
    if organization_id:
        rows = ad._merge_orphan_donations(
            organization_id,
            rows,
            campaigns,
            campaign_id=campaign_id,
            designation=None,
            status=status,
            frequency=frequency,
            date_from=resolved_from,
            date_to=resolved_to,
            select=_DONATION_SELECT,
        )
        if method_filter == "card":
            rows = [
                r
                for r in rows
                if not r.get("payment_method") or str(r.get("payment_method")).lower() == "card"
            ]
        elif method_filter:
            rows = [
                r for r in rows if str(r.get("payment_method") or "").lower() == method_filter
            ]

    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    if amount_sort:
        rows.sort(key=lambda r: ad._row_amount(r, reporting_currency), reverse=sort_desc)

    has_more = len(rows) > offset + limit
    page = rows[offset : offset + limit]

    total_amount = 0.0
    total_payout_amount = 0.0
    for row in page:
        ad._enrich_donation_fees(row)
        original_currency = str(row.get("currency") or "USD").upper()
        row["original_amount"] = float(row.get("amount") or 0)
        row["original_currency"] = original_currency
        row["reporting_amount"] = ad._row_amount(row, reporting_currency)
        row["reporting_currency"] = reporting_currency
        oid = str(row.get("organization_id") or "")
        if not oid and row.get("campaign_id"):
            camp = next((c for c in campaigns if str(c.get("id")) == str(row.get("campaign_id"))), None)
            oid = str((camp or {}).get("organization_id") or "")
            if oid:
                row["organization_id"] = oid
        row["organization_name"] = org_names.get(oid, "Unknown")
        payout = float(row.get("payout_amount") or 0)
        processing = float(row.get("processing_fee") or 0)
        platform = float(row.get("platform_fee") or 0)
        row["reporting_payout_amount"] = convert_to_reporting(
            payout, original_currency, reporting_currency
        )
        row["reporting_processing_fee"] = convert_to_reporting(
            processing, original_currency, reporting_currency
        )
        row["reporting_platform_fee"] = convert_to_reporting(
            platform, original_currency, reporting_currency
        )
        total_amount += float(row["reporting_amount"] or 0)
        total_payout_amount += float(row["reporting_payout_amount"] or 0)

    ad._attach_last_emails(page)

    return {
        "donations": page,
        "has_more": has_more,
        "total_amount": round(total_amount, 2),
        "total_payout_amount": round(total_payout_amount, 2),
        "reporting_currency": reporting_currency,
        "filter_options": _filter_options(org_names, campaigns),
    }


@router.get("/donations/{donation_id}")
def platform_donation_detail(
    donation_id: str,
    user: Annotated[AuthUser, Depends(require_super_admin)],
    reporting_currency: str = Query("USD"),
) -> dict[str, Any]:
    reporting_currency = (reporting_currency or "USD").strip().upper() or "USD"
    donation = rest_get_one(
        "donations",
        params={"id": f"eq.{donation_id}", "select": "*"},
    )
    if not donation:
        raise HTTPException(status_code=404, detail="Donation not found")

    donation = ad._enrich_donation_fees(donation)
    original_currency = str(donation.get("currency") or "USD").upper()
    donation["original_amount"] = float(donation.get("amount") or 0)
    donation["original_currency"] = original_currency
    donation["reporting_amount"] = ad._row_amount(donation, reporting_currency)
    donation["reporting_currency"] = reporting_currency

    org_names = _org_name_map()
    oid = str(donation.get("organization_id") or "")
    donation["organization_name"] = org_names.get(oid, "Unknown")

    campaign = None
    if donation.get("campaign_id"):
        campaign = rest_get_one(
            "campaigns",
            params={
                "id": f"eq.{donation['campaign_id']}",
                "select": "id,name,slug,designation,organization_id",
            },
        )
        if campaign and not oid:
            oid = str(campaign.get("organization_id") or "")
            donation["organization_id"] = oid
            donation["organization_name"] = org_names.get(oid, "Unknown")

    emails = rest_get(
        "email_logs",
        params={"donation_id": f"eq.{donation_id}", "select": "*", "order": "sent_at.desc"},
    )
    return {"donation": donation, "campaign": campaign, "emails": emails}


@router.get("/insights")
def platform_insights(
    user: Annotated[AuthUser, Depends(require_super_admin)],
    campaign_id: str | None = Query(None),
    organization_id: str | None = Query(None),
    designation: str | None = Query(None),
    utm_source: str | None = Query(None),
    frequency: str | None = Query(None),
    date_preset: str = Query("all"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    interval: str = Query("hourly"),
    reporting_currency: str = Query("USD"),
    timezone: str | None = Query(None),
) -> dict[str, Any]:
    reporting_currency = (reporting_currency or "USD").strip().upper() or "USD"
    tz_name = ad._org_zone(timezone or _DEFAULT_PLATFORM_TZ).key
    org_names = _org_name_map()
    campaigns = _all_campaigns(organization_id)
    filter_opts = _filter_options(org_names, campaigns, include_sources=True)

    resolved_from, resolved_to = ad._insights_date_range(
        date_preset, date_from, date_to, tz_name
    )

    if designation and not campaign_id:
        matching = [c["id"] for c in campaigns if c.get("designation") == designation]
        if not matching:
            empty = ad._empty_insights(
                reporting_currency,
                date_preset,
                campaigns,
                date_from=date_from,
                date_to=date_to,
                tz_name=tz_name,
            )
            empty["filter_options"] = {
                **filter_opts,
                "campaigns": filter_opts["campaigns"],
            }
            return empty

    params: dict[str, str] = {
        "select": _INSIGHTS_SELECT,
        "order": "created_at.desc",
        "limit": "10000",
    }
    if organization_id:
        params["organization_id"] = f"eq.{organization_id}"
    if campaign_id:
        params["campaign_id"] = f"eq.{campaign_id}"
    elif designation:
        matching = [str(c["id"]) for c in campaigns if c.get("designation") == designation]
        if matching:
            params["campaign_id"] = f"in.({','.join(matching)})"
    if frequency and frequency in {"once", "monthly"}:
        params["frequency"] = f"eq.{frequency}"
    if resolved_from and resolved_to:
        params["and"] = f"(created_at.gte.{resolved_from},created_at.lte.{resolved_to})"
    elif resolved_from:
        params["created_at"] = f"gte.{resolved_from}"
    elif resolved_to:
        params["created_at"] = f"lte.{resolved_to}"

    rows = rest_get("donations", params=params)
    if organization_id:
        rows = ad._merge_orphan_donations(
            organization_id,
            rows,
            campaigns,
            campaign_id=campaign_id,
            designation=designation,
            status=None,
            frequency=frequency,
            date_from=resolved_from,
            date_to=resolved_to,
            select=_INSIGHTS_SELECT,
        )

    if utm_source:
        rows = [
            r
            for r in rows
            if isinstance(r.get("utm"), dict) and r["utm"].get("source") == utm_source
        ]

    rows = ad._insights_countable(rows)

    recurring = [r for r in rows if r.get("frequency") == "monthly"]
    one_time = [r for r in rows if r.get("frequency") != "monthly"]
    total_raised = sum(ad._row_amount(r, reporting_currency) for r in rows)
    first_installments = sum(ad._row_amount(r, reporting_currency) for r in recurring)
    one_time_total = sum(ad._row_amount(r, reporting_currency) for r in one_time)

    campaign_name_by_id = {
        str(c["id"]): c.get("name", "Unknown") for c in campaigns if c.get("id")
    }
    chart = ad._build_chart(rows, interval, reporting_currency, tz_name)
    payment_methods = ad._breakdown(
        rows,
        lambda r: (r.get("payment_method") or "card").replace("_", " "),
        reporting_currency,
    )
    campaign_breakdown = ad._breakdown(
        rows,
        lambda r: campaign_name_by_id.get(str(r.get("campaign_id") or ""), "Unknown"),
        reporting_currency,
    )
    hour_breakdown = ad._breakdown(
        rows,
        lambda r: ad._hour_label(r.get("created_at", ""), tz_name),
        reporting_currency,
    )
    homepage_rows = [r for r in rows if ad._donation_checkout_view(r) == "homepage"]
    popup_rows = [r for r in rows if ad._donation_checkout_view(r) == "popup"]

    return {
        "reporting_currency": reporting_currency,
        "date_label": ad._date_label(date_preset, date_from, date_to, tz_name),
        "raised": {"total": round(total_raised, 2), "count": len(rows)},
        "first_installments": {"total": round(first_installments, 2), "count": len(recurring)},
        "one_time": {"total": round(one_time_total, 2), "count": len(one_time)},
        "chart": chart,
        "first_installments_chart": ad._build_chart(
            recurring, interval, reporting_currency, tz_name
        ),
        "one_time_chart": ad._build_chart(one_time, interval, reporting_currency, tz_name),
        "avg_donation": round(total_raised / len(rows), 2) if rows else 0,
        "retention_rate": round((len(recurring) / len(rows)) * 100, 1) if rows else 0,
        "payment_methods": payment_methods,
        "tribute_count": sum(1 for r in rows if r.get("honoree_name")),
        "comment_count": sum(1 for r in rows if r.get("comment")),
        "campaign_breakdown": campaign_breakdown,
        "hour_breakdown": hour_breakdown,
        "country_breakdown_homepage": ad._breakdown(
            homepage_rows, ad._donation_country_label, reporting_currency
        ),
        "country_breakdown_popup": ad._breakdown(
            popup_rows, ad._donation_country_label, reporting_currency
        ),
        "device_breakdown_homepage": ad._breakdown(
            homepage_rows, ad._donation_device_label, reporting_currency
        ),
        "device_breakdown_popup": ad._breakdown(
            popup_rows, ad._donation_device_label, reporting_currency
        ),
        "filter_options": {
            "organizations": filter_opts["organizations"],
            "campaigns": filter_opts["campaigns"],
            "designations": filter_opts["designations"],
            "sources": filter_opts["sources"],
        },
    }


def _merge_ga_totals(acc: dict[str, float], totals: dict[str, Any]) -> None:
    for key in (
        "sessions",
        "users",
        "new_users",
        "page_views",
        "engaged_sessions",
        "events",
    ):
        acc[key] = float(acc.get(key) or 0) + float(totals.get(key) or 0)
    # Weighted averages need counts — store sum*weight separately via sessions
    br = float(totals.get("bounce_rate") or 0)
    dur = float(totals.get("avg_session_duration") or 0)
    sessions = float(totals.get("sessions") or 0)
    acc["_bounce_weight"] = float(acc.get("_bounce_weight") or 0) + br * sessions
    acc["_duration_weight"] = float(acc.get("_duration_weight") or 0) + dur * sessions
    acc["_weight_sessions"] = float(acc.get("_weight_sessions") or 0) + sessions


def _finalize_ga_totals(acc: dict[str, float]) -> dict[str, float]:
    weight = float(acc.get("_weight_sessions") or 0)
    bounce = (float(acc.get("_bounce_weight") or 0) / weight) if weight else 0.0
    duration = (float(acc.get("_duration_weight") or 0) / weight) if weight else 0.0
    return {
        "sessions": acc.get("sessions", 0),
        "users": acc.get("users", 0),
        "new_users": acc.get("new_users", 0),
        "page_views": acc.get("page_views", 0),
        "bounce_rate": bounce,
        "avg_session_duration": duration,
        "engaged_sessions": acc.get("engaged_sessions", 0),
        "events": acc.get("events", 0),
    }


def _merge_keyed_rows(
    buckets: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
    key_fields: tuple[str, ...],
    sum_fields: tuple[str, ...],
) -> None:
    for row in rows:
        key = "|".join(str(row.get(f) or "") for f in key_fields)
        bucket = buckets.setdefault(key, {f: row.get(f) for f in key_fields})
        for field in sum_fields:
            bucket[field] = float(bucket.get(field) or 0) + float(row.get(field) or 0)


@router.get("/google-analytics")
def platform_google_analytics(
    user: Annotated[AuthUser, Depends(require_super_admin)],
    date_preset: str = Query("30d"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    organization_id: str | None = Query(None),
    campaign_id: str | None = Query(None),
    bootstrap: bool = Query(False),
) -> dict[str, Any]:
    from ga4_client import fetch_dashboard, fetch_realtime_snapshot, ga4_configured

    org_names = _org_name_map()
    campaigns = _all_campaigns(organization_id)
    campaign_ids = [str(c["id"]) for c in campaigns if c.get("id")]
    content_by_id: dict[str, dict[str, Any]] = {}
    if campaign_ids:
        # Chunk in() filters to avoid URL length limits
        for i in range(0, len(campaign_ids), 80):
            chunk = campaign_ids[i : i + 80]
            contents = rest_get(
                "campaign_content",
                params={
                    "campaign_id": f"in.({','.join(chunk)})",
                    "select": "campaign_id,ga4_measurement_id,gtm_container_id,ga4_property_id",
                    "limit": "200",
                },
            )
            for row in contents:
                cid = str(row.get("campaign_id") or "")
                if cid:
                    content_by_id[cid] = row

    campaign_options = []
    for campaign in campaigns:
        cid = str(campaign.get("id") or "")
        content = content_by_id.get(cid) or {}
        prop = str(content.get("ga4_property_id") or "").strip().replace("properties/", "")
        campaign_options.append(
            {
                "id": cid,
                "title": campaign.get("name") or "Campaign",
                "slug": campaign.get("slug") or "",
                "organization_id": str(campaign.get("organization_id") or ""),
                "organization_name": org_names.get(
                    str(campaign.get("organization_id") or ""), ""
                ),
                "ga4_measurement_id": content.get("ga4_measurement_id") or "",
                "gtm_container_id": content.get("gtm_container_id") or "",
                "ga4_property_id": prop,
            }
        )

    if campaign_id:
        campaign_options_filtered = [c for c in campaign_options if c["id"] == campaign_id]
    else:
        campaign_options_filtered = campaign_options

    with_prop = [c for c in campaign_options_filtered if c.get("ga4_property_id")]
    organizations = [
        {"id": oid, "name": name}
        for oid, name in sorted(org_names.items(), key=lambda item: item[1].lower())
    ]

    start, end = ad._ga4_date_range(date_preset, date_from, date_to)
    configured = ga4_configured()

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
        "property_id": "",
        "date_preset": date_preset,
        "date_from": start,
        "date_to": end,
        "campaigns": campaign_options,
        "organizations": organizations,
        "selected_campaign_id": campaign_id or "",
        "selected_organization_id": organization_id or "",
        "properties_included": 0,
        "properties_available": 0,
        "truncated": False,
        "setup": {
            "needs_service_account": not configured,
            "needs_property_id": configured and not with_prop,
            "hint": (
                "Grant the platform GA4 service account Viewer on each campaign's GA4 property, "
                "and save a GA4 Property ID on campaign Content tabs."
            ),
        },
        **empty_report,
    }

    if bootstrap:
        return payload

    # Unique properties from filtered campaigns
    prop_to_campaigns: dict[str, list[dict[str, Any]]] = {}
    for c in with_prop:
        prop = str(c["ga4_property_id"])
        prop_to_campaigns.setdefault(prop, []).append(c)

    unique_props = list(prop_to_campaigns.keys())
    payload["properties_available"] = len(unique_props)
    truncated = len(unique_props) > _GA_PROPERTY_CAP
    unique_props = unique_props[:_GA_PROPERTY_CAP]
    payload["truncated"] = truncated
    payload["properties_included"] = len(unique_props)

    if not configured or not unique_props:
        return payload

    errors: list[str] = []
    totals_acc: dict[str, float] = {}
    ts_buckets: dict[str, dict[str, Any]] = {}
    pages: dict[str, dict[str, Any]] = {}
    sources: dict[str, dict[str, Any]] = {}
    devices: dict[str, dict[str, Any]] = {}
    countries: dict[str, dict[str, Any]] = {}
    events: dict[str, dict[str, Any]] = {}
    realtime_acc = {"active_users": 0, "page_views": 0, "events": 0}

    def _fetch_one(prop: str) -> dict[str, Any] | None:
        try:
            return fetch_dashboard(date_from=start, date_to=end, property_id=prop)
        except Exception as exc:
            errors.append(f"{prop}: {exc}")
            return None

    with ThreadPoolExecutor(max_workers=min(5, len(unique_props))) as pool:
        futures = {pool.submit(_fetch_one, prop): prop for prop in unique_props}
        for fut in as_completed(futures):
            report = fut.result()
            if not report:
                continue
            _merge_ga_totals(totals_acc, report.get("totals") or {})
            _merge_keyed_rows(
                ts_buckets,
                report.get("timeseries") or [],
                ("date",),
                ("sessions", "users", "page_views", "events"),
            )
            _merge_keyed_rows(
                pages,
                report.get("top_pages") or [],
                ("path",),
                ("page_views", "sessions", "users"),
            )
            _merge_keyed_rows(
                sources,
                report.get("sources") or [],
                ("source", "medium"),
                ("sessions", "users"),
            )
            _merge_keyed_rows(
                devices,
                report.get("devices") or [],
                ("device",),
                ("sessions", "users"),
            )
            _merge_keyed_rows(
                countries,
                report.get("countries") or [],
                ("country",),
                ("sessions", "users"),
            )
            _merge_keyed_rows(
                events,
                report.get("events") or [],
                ("name",),
                ("count", "users"),
            )

    # Realtime only for "today"-like presets
    if date_preset == "today" or (start == "today" and end == "today"):
        for prop in unique_props[:5]:
            try:
                snap = fetch_realtime_snapshot(property_id=prop)
                realtime_acc["active_users"] += int(snap.get("active_users") or 0)
                realtime_acc["page_views"] += int(snap.get("page_views") or 0)
                realtime_acc["events"] += int(snap.get("events") or 0)
            except Exception:
                pass
        payload["realtime"] = realtime_acc
        payload["today_note"] = (
            "Combined realtime across included properties (last ~30 minutes)."
            if realtime_acc["active_users"] or realtime_acc["page_views"]
            else None
        )
    else:
        payload["realtime"] = None
        payload["today_note"] = None

    payload["configured"] = True
    payload["property_id"] = ",".join(unique_props[:3]) + ("…" if len(unique_props) > 3 else "")
    payload["totals"] = _finalize_ga_totals(totals_acc)
    payload["timeseries"] = sorted(ts_buckets.values(), key=lambda r: str(r.get("date") or ""))
    payload["top_pages"] = sorted(
        pages.values(), key=lambda r: float(r.get("page_views") or 0), reverse=True
    )[:20]
    payload["sources"] = sorted(
        sources.values(), key=lambda r: float(r.get("sessions") or 0), reverse=True
    )[:15]
    payload["devices"] = sorted(
        devices.values(), key=lambda r: float(r.get("sessions") or 0), reverse=True
    )
    payload["countries"] = sorted(
        countries.values(), key=lambda r: float(r.get("sessions") or 0), reverse=True
    )[:15]
    payload["events"] = sorted(
        events.values(), key=lambda r: float(r.get("count") or 0), reverse=True
    )[:25]
    payload["error"] = "; ".join(errors[:3]) if errors else None
    if truncated:
        payload["today_note"] = (
            (payload.get("today_note") or "")
            + f" Showing {_GA_PROPERTY_CAP} of {payload['properties_available']} GA4 properties — narrow filters to include more."
        ).strip()
    return payload
