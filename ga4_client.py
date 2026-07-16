"""Google Analytics 4 Data API client (service-account auth)."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

GA4_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
GA4_DATA_BASE = "https://analyticsdata.googleapis.com/v1beta"


def get_property_id() -> str | None:
    return _property_id()


def service_account_ready() -> bool:
    return bool(_service_account_info())


def ga4_configured() -> bool:
    """True when a service account is available (property ID may come from campaigns)."""
    return service_account_ready()


def _property_id() -> str | None:
    raw = (os.getenv("GA4_PROPERTY_ID") or "").strip()
    if not raw:
        return None
    # Accept "properties/123" or bare "123"
    return raw.replace("properties/", "").strip() or None


def _service_account_info() -> dict[str, Any] | None:
    raw_json = (os.getenv("GA4_SERVICE_ACCOUNT_JSON") or "").strip()
    if raw_json:
        try:
            data = json.loads(raw_json)
            if isinstance(data, dict) and data.get("client_email"):
                return data
        except json.JSONDecodeError:
            return None

    path = (os.getenv("GA4_SERVICE_ACCOUNT_FILE") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and data.get("client_email"):
                return data
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _access_token() -> str:
    info = _service_account_info()
    if not info:
        raise RuntimeError("GA4 service account is not configured")

    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
    except ImportError as exc:
        raise RuntimeError(
            "Install google-auth to use Google Analytics (pip install google-auth)"
        ) from exc

    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=[GA4_SCOPE],
    )
    credentials.refresh(Request())
    if not credentials.token:
        raise RuntimeError("Failed to obtain Google Analytics access token")
    return str(credentials.token)


def run_report(
    *,
    property_id: str | None = None,
    date_from: str,
    date_to: str,
    dimensions: list[str] | None = None,
    metrics: list[str],
    dimension_filter: dict[str, Any] | None = None,
    order_bys: list[dict[str, Any]] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    prop = (property_id or _property_id() or "").replace("properties/", "").strip()
    if not prop:
        raise RuntimeError("GA4_PROPERTY_ID is not set")

    body: dict[str, Any] = {
        "dateRanges": [{"startDate": date_from, "endDate": date_to}],
        "metrics": [{"name": name} for name in metrics],
        "limit": str(limit),
    }
    if dimensions:
        body["dimensions"] = [{"name": name} for name in dimensions]
    if dimension_filter:
        body["dimensionFilter"] = dimension_filter
    if order_bys:
        body["orderBys"] = order_bys

    token = _access_token()
    url = f"{GA4_DATA_BASE}/properties/{prop}:runReport"
    with httpx.Client(timeout=45.0) as client:
        response = client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
        )
    if response.status_code >= 400:
        detail = response.text
        try:
            payload = response.json()
            detail = payload.get("error", {}).get("message") or detail
        except Exception:
            pass
        raise RuntimeError(f"GA4 API error ({response.status_code}): {detail}")
    return response.json()


def run_realtime_report(
    *,
    property_id: str | None = None,
    metrics: list[str],
    dimensions: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """GA4 Realtime API — last ~30 minutes (shows visits before daily reports catch up)."""
    prop = (property_id or _property_id() or "").replace("properties/", "").strip()
    if not prop:
        raise RuntimeError("GA4_PROPERTY_ID is not set")

    body: dict[str, Any] = {
        "metrics": [{"name": name} for name in metrics],
        "limit": str(limit),
    }
    if dimensions:
        body["dimensions"] = [{"name": name} for name in dimensions]

    token = _access_token()
    url = f"{GA4_DATA_BASE}/properties/{prop}:runRealtimeReport"
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
        )
    if response.status_code >= 400:
        detail = response.text
        try:
            payload = response.json()
            detail = payload.get("error", {}).get("message") or detail
        except Exception:
            pass
        raise RuntimeError(f"GA4 Realtime API error ({response.status_code}): {detail}")
    return response.json()


def fetch_realtime_snapshot(*, property_id: str | None = None) -> dict[str, Any]:
    """Active users + page views in the last ~30 minutes."""
    try:
        summary = run_realtime_report(
            property_id=property_id,
            metrics=["activeUsers", "screenPageViews", "eventCount"],
            limit=1,
        )
        rows = parse_rows(summary)
        totals = rows[0] if rows else {}
        # Realtime totals sometimes land in metricHeaders only with empty rows
        if not totals and summary.get("totals"):
            headers = [h.get("name", "") for h in summary.get("metricHeaders") or []]
            values = (summary.get("totals") or [{}])[0].get("metricValues") or []
            for index, name in enumerate(headers):
                raw = (values[index] or {}).get("value") if index < len(values) else "0"
                try:
                    totals[name] = float(raw or 0)
                except (TypeError, ValueError):
                    totals[name] = 0.0
        return {
            "active_users": totals.get("activeUsers", 0),
            "page_views": totals.get("screenPageViews", 0),
            "events": totals.get("eventCount", 0),
        }
    except Exception as exc:
        return {
            "active_users": 0,
            "page_views": 0,
            "events": 0,
            "error": str(exc),
        }


def _metric_map(row: dict[str, Any], metric_headers: list[str]) -> dict[str, float]:
    values = row.get("metricValues") or []
    out: dict[str, float] = {}
    for index, name in enumerate(metric_headers):
        raw = (values[index] or {}).get("value") if index < len(values) else "0"
        try:
            out[name] = float(raw or 0)
        except (TypeError, ValueError):
            out[name] = 0.0
    return out


def _dimension_map(row: dict[str, Any], dimension_headers: list[str]) -> dict[str, str]:
    values = row.get("dimensionValues") or []
    out: dict[str, str] = {}
    for index, name in enumerate(dimension_headers):
        raw = (values[index] or {}).get("value") if index < len(values) else ""
        out[name] = str(raw or "")
    return out


def parse_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    metric_headers = [h.get("name", "") for h in report.get("metricHeaders") or []]
    dimension_headers = [h.get("name", "") for h in report.get("dimensionHeaders") or []]
    rows: list[dict[str, Any]] = []
    for row in report.get("rows") or []:
        item = {
            **_dimension_map(row, dimension_headers),
            **_metric_map(row, metric_headers),
        }
        rows.append(item)
    return rows


def fetch_dashboard(
    *,
    date_from: str,
    date_to: str,
    property_id: str | None = None,
    path_contains: str | None = None,
) -> dict[str, Any]:
    """Fetch summary + key breakdowns for the admin Google Analytics page."""
    dim_filter = None
    if path_contains and path_contains.strip():
        dim_filter = {
            "filter": {
                "fieldName": "pagePath",
                "stringFilter": {
                    "matchType": "CONTAINS",
                    "value": path_contains.strip(),
                    "caseSensitive": False,
                },
            }
        }

    summary = run_report(
        property_id=property_id,
        date_from=date_from,
        date_to=date_to,
        metrics=[
            "sessions",
            "totalUsers",
            "newUsers",
            "screenPageViews",
            "bounceRate",
            "averageSessionDuration",
            "engagedSessions",
            "eventCount",
        ],
        dimension_filter=dim_filter,
        limit=1,
    )

    timeseries = run_report(
        property_id=property_id,
        date_from=date_from,
        date_to=date_to,
        dimensions=["date"],
        metrics=["sessions", "totalUsers", "screenPageViews", "eventCount"],
        dimension_filter=dim_filter,
        order_bys=[{"dimension": {"dimensionName": "date"}}],
        limit=400,
    )

    top_pages = run_report(
        property_id=property_id,
        date_from=date_from,
        date_to=date_to,
        dimensions=["pagePath"],
        metrics=["screenPageViews", "sessions", "totalUsers"],
        dimension_filter=dim_filter,
        order_bys=[{"metric": {"metricName": "screenPageViews"}, "desc": True}],
        limit=15,
    )

    sources = run_report(
        property_id=property_id,
        date_from=date_from,
        date_to=date_to,
        dimensions=["sessionSource", "sessionMedium"],
        metrics=["sessions", "totalUsers"],
        dimension_filter=dim_filter,
        order_bys=[{"metric": {"metricName": "sessions"}, "desc": True}],
        limit=12,
    )

    devices = run_report(
        property_id=property_id,
        date_from=date_from,
        date_to=date_to,
        dimensions=["deviceCategory"],
        metrics=["sessions", "totalUsers"],
        dimension_filter=dim_filter,
        order_bys=[{"metric": {"metricName": "sessions"}, "desc": True}],
        limit=10,
    )

    countries = run_report(
        property_id=property_id,
        date_from=date_from,
        date_to=date_to,
        dimensions=["country"],
        metrics=["sessions", "totalUsers"],
        dimension_filter=dim_filter,
        order_bys=[{"metric": {"metricName": "sessions"}, "desc": True}],
        limit=12,
    )

    events = run_report(
        property_id=property_id,
        date_from=date_from,
        date_to=date_to,
        dimensions=["eventName"],
        metrics=["eventCount", "totalUsers"],
        dimension_filter=dim_filter,
        order_bys=[{"metric": {"metricName": "eventCount"}, "desc": True}],
        limit=20,
    )

    summary_rows = parse_rows(summary)
    totals = summary_rows[0] if summary_rows else {}

    return {
        "property_id": (property_id or _property_id()),
        "date_from": date_from,
        "date_to": date_to,
        "totals": {
            "sessions": totals.get("sessions", 0),
            "users": totals.get("totalUsers", 0),
            "new_users": totals.get("newUsers", 0),
            "page_views": totals.get("screenPageViews", 0),
            "bounce_rate": totals.get("bounceRate", 0),
            "avg_session_duration": totals.get("averageSessionDuration", 0),
            "engaged_sessions": totals.get("engagedSessions", 0),
            "events": totals.get("eventCount", 0),
        },
        "timeseries": [
            {
                "date": row.get("date", ""),
                "sessions": row.get("sessions", 0),
                "users": row.get("totalUsers", 0),
                "page_views": row.get("screenPageViews", 0),
                "events": row.get("eventCount", 0),
            }
            for row in parse_rows(timeseries)
        ],
        "top_pages": [
            {
                "path": row.get("pagePath", ""),
                "page_views": row.get("screenPageViews", 0),
                "sessions": row.get("sessions", 0),
                "users": row.get("totalUsers", 0),
            }
            for row in parse_rows(top_pages)
        ],
        "sources": [
            {
                "source": row.get("sessionSource", ""),
                "medium": row.get("sessionMedium", ""),
                "sessions": row.get("sessions", 0),
                "users": row.get("totalUsers", 0),
            }
            for row in parse_rows(sources)
        ],
        "devices": [
            {
                "device": row.get("deviceCategory", ""),
                "sessions": row.get("sessions", 0),
                "users": row.get("totalUsers", 0),
            }
            for row in parse_rows(devices)
        ],
        "countries": [
            {
                "country": row.get("country", ""),
                "sessions": row.get("sessions", 0),
                "users": row.get("totalUsers", 0),
            }
            for row in parse_rows(countries)
        ],
        "events": [
            {
                "name": row.get("eventName", ""),
                "count": row.get("eventCount", 0),
                "users": row.get("totalUsers", 0),
            }
            for row in parse_rows(events)
        ],
    }
