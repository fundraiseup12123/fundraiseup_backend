"""Ad Spend & ROI Tracking router for Platform Super-Admin.

Manages ad campaigns (Google Ads, Meta Ads, TikTok Ads) with effective-date
budget tracking, computes daily Net Remaining = Gross Raised - Spend, ROAS, CPA,
and generates performance reports & CSV exports with zero impact on existing features.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from auth import AuthUser, require_super_admin
from currency import convert_to_reporting
from routers import admin_data as ad
import httpx
from db import rest_delete, rest_get, rest_get_one, rest_insert, rest_patch, supabase_url, _headers, supabase_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/super", tags=["ad-spend"])

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOCAL_STORE_PATH = DATA_DIR / "ad_spend_store.json"

_DB_CHECK_CACHE: dict[str, Any] = {"checked_at": 0.0, "exists": False}
_DEFAULT_PLATFORM_TZ = "America/Los_Angeles"


# ---------------------------------------------------------------------------
# Resilient Storage: Supabase with Automatic Local Fallback
# ---------------------------------------------------------------------------

def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _supabase_ad_tables_exist() -> bool:
    import time
    now = time.time()
    if now - _DB_CHECK_CACHE["checked_at"] < 30.0:
        return bool(_DB_CHECK_CACHE["exists"])

    if not supabase_enabled():
        _DB_CHECK_CACHE["checked_at"] = now
        _DB_CHECK_CACHE["exists"] = False
        return False

    try:
        r = httpx.get(
            f"{supabase_url()}/rest/v1/ad_campaigns",
            headers=_headers(),
            params={"limit": "1"},
            timeout=4.0,
        )
        exists = bool(r.status_code < 400)
        _DB_CHECK_CACHE["checked_at"] = now
        _DB_CHECK_CACHE["exists"] = exists
        return exists
    except Exception:
        _DB_CHECK_CACHE["checked_at"] = now
        _DB_CHECK_CACHE["exists"] = False
        return False


def _load_local_store() -> dict[str, list[dict[str, Any]]]:
    _ensure_data_dir()
    if not LOCAL_STORE_PATH.exists():
        initial = {
            "campaigns": [
                {
                    "id": "c1010101-0000-4000-8000-000000000001",
                    "name": "Meta Ads - Gaza Emergency Relief",
                    "channel": "meta",
                    "utm_campaign": "gaza_meta_ads",
                    "utm_source": "facebook",
                    "campaign_id": None,
                    "status": "active",
                    "notes": "Main Meta prospecting & retargeting campaign",
                    "created_at": "2026-08-01T00:00:00+00:00",
                    "updated_at": "2026-08-01T00:00:00+00:00",
                },
                {
                    "id": "c1010101-0000-4000-8000-000000000002",
                    "name": "Google Search - Urgent Food & Medical",
                    "channel": "google",
                    "utm_campaign": "google_search_gaza",
                    "utm_source": "google",
                    "campaign_id": None,
                    "status": "active",
                    "notes": "High-intent search keywords",
                    "created_at": "2026-08-01T00:00:00+00:00",
                    "updated_at": "2026-08-01T00:00:00+00:00",
                },
                {
                    "id": "c1010101-0000-4000-8000-000000000003",
                    "name": "TikTok Ads - Viral Video Relief",
                    "channel": "tiktok",
                    "utm_campaign": "tiktok_relief",
                    "utm_source": "tiktok",
                    "campaign_id": None,
                    "status": "active",
                    "notes": "Short video engagement ads",
                    "created_at": "2026-08-15T00:00:00+00:00",
                    "updated_at": "2026-08-15T00:00:00+00:00",
                },
            ],
            "budgets": [
                {
                    "id": "b1010101-0000-4000-8000-000000000001",
                    "ad_campaign_id": "c1010101-0000-4000-8000-000000000001",
                    "daily_budget": 50.0,
                    "currency": "USD",
                    "start_date": "2026-08-01",
                    "end_date": None,
                    "created_at": "2026-08-01T00:00:00+00:00",
                },
                {
                    "id": "b1010101-0000-4000-8000-000000000002",
                    "ad_campaign_id": "c1010101-0000-4000-8000-000000000002",
                    "daily_budget": 40.0,
                    "currency": "USD",
                    "start_date": "2026-08-01",
                    "end_date": None,
                    "created_at": "2026-08-01T00:00:00+00:00",
                },
                {
                    "id": "b1010101-0000-4000-8000-000000000003",
                    "ad_campaign_id": "c1010101-0000-4000-8000-000000000003",
                    "daily_budget": 30.0,
                    "currency": "USD",
                    "start_date": "2026-08-15",
                    "end_date": None,
                    "created_at": "2026-08-15T00:00:00+00:00",
                },
            ],
        }
        with open(LOCAL_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(initial, f, indent=2)
        return initial

    try:
        with open(LOCAL_STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Error reading local ad spend store: %s", exc)
        return {"campaigns": [], "budgets": []}


def _save_local_store(data: dict[str, list[dict[str, Any]]]) -> None:
    _ensure_data_dir()
    temp_path = LOCAL_STORE_PATH.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    temp_path.replace(LOCAL_STORE_PATH)



# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

class CreateAdCampaignRequest(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    channel: Literal["google", "meta", "tiktok", "pinterest", "other"]
    daily_budget: float = Field(ge=0)
    currency: str = Field(default="USD", max_length=10)
    utm_campaign: str | None = Field(default=None, max_length=255)
    utm_source: str | None = Field(default=None, max_length=100)
    campaign_id: str | None = None
    status: Literal["active", "paused", "completed"] = "active"
    start_date: str | None = None
    notes: str | None = None


class UpdateAdCampaignRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=150)
    channel: Literal["google", "meta", "tiktok", "pinterest", "other"] | None = None
    utm_campaign: str | None = None
    utm_source: str | None = None
    campaign_id: str | None = None
    status: Literal["active", "paused", "completed"] | None = None
    notes: str | None = None


class UpdateBudgetRequest(BaseModel):
    daily_budget: float = Field(ge=0)
    currency: str = Field(default="USD", max_length=10)
    effective_date: str | None = None  # YYYY-MM-DD, defaults to today in timezone


class CustomBudgetPeriodRequest(BaseModel):
    daily_budget: float = Field(ge=0)
    currency: str = Field(default="USD", max_length=10)
    start_date: str  # YYYY-MM-DD
    end_date: str | None = None  # YYYY-MM-DD or None for open-ended


# ---------------------------------------------------------------------------
# Core Campaign & Budget Operations
# ---------------------------------------------------------------------------

def _get_campaigns_data() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Retrieve campaigns and budgets, using Supabase if available or local store."""
    if _supabase_ad_tables_exist():
        c_rows = rest_get("ad_campaigns", params={"order": "created_at.desc"})
        b_rows = rest_get("ad_campaign_budgets", params={"order": "start_date.asc"})
        for c in c_rows:
            notes = str(c.get("notes") or "")
            if c.get("channel") == "other" and "[channel:pinterest]" in notes:
                c["channel"] = "pinterest"
                cleaned = notes.replace("[channel:pinterest]", "").strip()
                c["notes"] = cleaned or None
        return c_rows, b_rows

    store = _load_local_store()
    return store.get("campaigns", []), store.get("budgets", [])


def _find_effective_budget_on_date(
    budgets: list[dict[str, Any]],
    campaign_id: str,
    target_date: str,  # "YYYY-MM-DD"
) -> dict[str, Any] | None:
    """Find the budget record active on target_date for a given campaign."""
    matching: list[dict[str, Any]] = []
    for b in budgets:
        if str(b.get("ad_campaign_id")) != str(campaign_id):
            continue
        start = str(b.get("start_date") or "")[:10]
        end = str(b.get("end_date") or "")[:10] if b.get("end_date") else None
        if start <= target_date and (not end or end >= target_date):
            matching.append(b)

    if not matching:
        return None
    # If multiple somehow overlap, pick the latest start_date
    matching.sort(key=lambda r: str(r.get("start_date") or ""), reverse=True)
    return matching[0]


def _current_active_budget(
    budgets: list[dict[str, Any]],
    campaign_id: str,
    today_str: str,
) -> dict[str, Any] | None:
    return _find_effective_budget_on_date(budgets, campaign_id, today_str)


def _param_str(val: Any, default: str = "") -> str:
    if isinstance(val, str):
        return val.strip()
    return default


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@router.get("/ad-campaigns")
def list_ad_campaigns(
    user: Annotated[AuthUser, Depends(require_super_admin)],
    channel: str | None = Query(None),
    status: str | None = Query(None),
    timezone: str | None = Query(None),
) -> dict[str, Any]:
    """List all ad campaigns with their current active budget."""
    tz_input = _param_str(timezone, "UTC") or "UTC"
    tz_name = ad._org_zone(tz_input).key
    now_tz = datetime.now(ad._org_zone(tz_name))
    today_str = now_tz.strftime("%Y-%m-%d")

    filter_channel = _param_str(channel, "")
    filter_status = _param_str(status, "")

    campaigns, budgets = _get_campaigns_data()

    result: list[dict[str, Any]] = []
    for c in campaigns:
        c_status = str(c.get("status") or "active")
        c_channel = str(c.get("channel") or "other")
        if filter_status and filter_status != "all" and c_status != filter_status:
            continue
        if filter_channel and filter_channel != "all" and c_channel != filter_channel:
            continue

        curr_budget = _current_active_budget(budgets, str(c["id"]), today_str)
        daily_amount = float(curr_budget.get("daily_budget") or 0.0) if curr_budget else 0.0
        currency = str(curr_budget.get("currency") or "USD") if curr_budget else "USD"

        result.append({
            **c,
            "current_daily_budget": daily_amount,
            "current_currency": currency,
            "effective_budget_id": curr_budget.get("id") if curr_budget else None,
            "effective_start_date": curr_budget.get("start_date") if curr_budget else None,
        })

    return {"campaigns": result, "today": today_str}


@router.post("/ad-campaigns")
def create_ad_campaign(
    payload: CreateAdCampaignRequest,
    user: Annotated[AuthUser, Depends(require_super_admin)],
    timezone: str | None = Query(None),
) -> dict[str, Any]:
    """Create a new ad campaign with an initial effective daily budget."""
    tz_input = _param_str(timezone, "UTC") or "UTC"
    tz_name = ad._org_zone(tz_input).key
    now_tz = datetime.now(ad._org_zone(tz_name))
    today_str = now_tz.strftime("%Y-%m-%d")
    start_date = payload.start_date.strip()[:10] if payload.start_date else today_str
    now_iso = datetime.now().astimezone().isoformat()

    campaign_id = str(uuid.uuid4())
    budget_id = str(uuid.uuid4())

    new_campaign = {
        "id": campaign_id,
        "name": payload.name.strip(),
        "channel": payload.channel,
        "utm_campaign": (payload.utm_campaign or "").strip() or None,
        "utm_source": (payload.utm_source or "").strip() or None,
        "campaign_id": payload.campaign_id or None,
        "status": payload.status,
        "notes": payload.notes or None,
        "created_at": now_iso,
        "updated_at": now_iso,
    }

    new_budget = {
        "id": budget_id,
        "ad_campaign_id": campaign_id,
        "daily_budget": round(float(payload.daily_budget), 2),
        "currency": payload.currency.strip().upper(),
        "start_date": start_date,
        "end_date": None,
        "created_at": now_iso,
    }

    if _supabase_ad_tables_exist():
        try:
            res_camp = rest_insert("ad_campaigns", new_campaign)
            if not res_camp and new_campaign.get("channel") == "pinterest":
                # Fallback if DB check constraint doesn't yet include 'pinterest'
                fallback_camp = dict(new_campaign)
                fallback_camp["channel"] = "other"
                fallback_camp["notes"] = f"[channel:pinterest] {new_campaign.get('notes') or ''}".strip()
                res_camp = rest_insert("ad_campaigns", fallback_camp)

            if res_camp:
                rest_insert("ad_campaign_budgets", new_budget)
                return {
                    "campaign": {
                        **new_campaign,
                        "current_daily_budget": new_budget["daily_budget"],
                        "current_currency": new_budget["currency"],
                    },
                    "budget": new_budget,
                }
        except Exception as exc:
            logger.warning("Supabase insert failed for ad campaign, using local store: %s", exc)

    # Local fallback
    store = _load_local_store()
    store.setdefault("campaigns", []).insert(0, new_campaign)
    store.setdefault("budgets", []).append(new_budget)
    _save_local_store(store)

    return {
        "campaign": {
            **new_campaign,
            "current_daily_budget": new_budget["daily_budget"],
            "current_currency": new_budget["currency"],
        },
        "budget": new_budget,
    }


@router.patch("/ad-campaigns/{campaign_id}")
def update_ad_campaign(
    campaign_id: str,
    payload: UpdateAdCampaignRequest,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    """Update campaign metadata (name, channel, tags, status, notes)."""
    now_iso = datetime.now().astimezone().isoformat()
    patch_fields: dict[str, Any] = {"updated_at": now_iso}

    if payload.name is not None:
        patch_fields["name"] = payload.name.strip()
    if payload.channel is not None:
        patch_fields["channel"] = payload.channel
    if payload.utm_campaign is not None:
        patch_fields["utm_campaign"] = payload.utm_campaign.strip() or None
    if payload.utm_source is not None:
        patch_fields["utm_source"] = payload.utm_source.strip() or None
    if payload.campaign_id is not None:
        patch_fields["campaign_id"] = payload.campaign_id or None
    if payload.status is not None:
        patch_fields["status"] = payload.status
    if payload.notes is not None:
        patch_fields["notes"] = payload.notes or None

    if _supabase_ad_tables_exist():
        try:
            res = rest_patch("ad_campaigns", patch_fields, match={"id": campaign_id})
            if not res and patch_fields.get("channel") == "pinterest":
                fallback_patch = dict(patch_fields)
                fallback_patch["channel"] = "other"
                existing = rest_get_one("ad_campaigns", params={"id": f"eq.{campaign_id}"}) or {}
                ex_notes = str(existing.get("notes") or "")
                if "[channel:pinterest]" not in ex_notes:
                    fallback_patch["notes"] = f"[channel:pinterest] {ex_notes}".strip()
                rest_patch("ad_campaigns", fallback_patch, match={"id": campaign_id})

            row = rest_get_one("ad_campaigns", params={"id": f"eq.{campaign_id}"})
            if row:
                notes = str(row.get("notes") or "")
                if row.get("channel") == "other" and "[channel:pinterest]" in notes:
                    row["channel"] = "pinterest"
                    row["notes"] = notes.replace("[channel:pinterest]", "").strip() or None
                return {"campaign": row}
        except Exception as exc:
            logger.warning("Supabase update failed for ad campaign %s: %s", campaign_id, exc)

    store = _load_local_store()
    for c in store.get("campaigns", []):
        if str(c.get("id")) == str(campaign_id):
            c.update(patch_fields)
            _save_local_store(store)
            return {"campaign": c}

    raise HTTPException(status_code=404, detail="Ad campaign not found")


@router.post("/ad-campaigns/{campaign_id}/budget")
def update_campaign_budget(
    campaign_id: str,
    payload: UpdateBudgetRequest,
    user: Annotated[AuthUser, Depends(require_super_admin)],
    timezone: str | None = Query(None),
) -> dict[str, Any]:
    """Update daily budget with effective dating.

    Historical dates preserve their original budget; new budget takes effect from effective_date forward.
    """
    tz_input = _param_str(timezone, "UTC") or "UTC"
    tz_name = ad._org_zone(tz_input).key
    now_tz = datetime.now(ad._org_zone(tz_name))
    today_str = now_tz.strftime("%Y-%m-%d")
    eff_date_str = payload.effective_date.strip()[:10] if payload.effective_date else today_str
    new_daily_budget = round(float(payload.daily_budget), 2)
    currency = payload.currency.strip().upper() or "USD"
    now_iso = datetime.now().astimezone().isoformat()

    try:
        eff_dt = datetime.strptime(eff_date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid effective_date format. Use YYYY-MM-DD.")

    yesterday_dt = eff_dt - timedelta(days=1)
    yesterday_str = yesterday_dt.strftime("%Y-%m-%d")

    campaigns, budgets = _get_campaigns_data()
    target_campaign = next((c for c in campaigns if str(c.get("id")) == str(campaign_id)), None)
    if not target_campaign:
        raise HTTPException(status_code=404, detail="Ad campaign not found")

    # Find budget currently active on effective_date
    existing_active = _find_effective_budget_on_date(budgets, campaign_id, eff_date_str)

    # Determine end date for the new budget entry:
    # If there are subsequent budgets starting after eff_date_str, close this entry before the next one starts
    later_budgets = [
        b for b in budgets
        if str(b.get("ad_campaign_id")) == str(campaign_id)
        and str(b.get("start_date") or "")[:10] > eff_date_str
    ]
    new_end_date = None
    if existing_active and existing_active.get("end_date"):
        new_end_date = str(existing_active.get("end_date") or "")[:10]
    elif later_budgets:
        later_budgets.sort(key=lambda r: str(r.get("start_date") or ""))
        next_start = str(later_budgets[0].get("start_date") or "")[:10]
        next_dt = datetime.strptime(next_start, "%Y-%m-%d").date() - timedelta(days=1)
        new_end_date = next_dt.strftime("%Y-%m-%d")

    if _supabase_ad_tables_exist():
        try:
            if existing_active:
                existing_start = str(existing_active.get("start_date") or "")[:10]
                if existing_start == eff_date_str:
                    # Same date: update in-place
                    rest_patch(
                        "ad_campaign_budgets",
                        {"daily_budget": new_daily_budget, "currency": currency},
                        match={"id": str(existing_active["id"])},
                    )
                    return {"message": "Budget updated for effective date", "start_date": eff_date_str}
                else:
                    # Close previous budget as of yesterday
                    rest_patch(
                        "ad_campaign_budgets",
                        {"end_date": yesterday_str},
                        match={"id": str(existing_active["id"])},
                    )

            # Insert new effective budget
            new_id = str(uuid.uuid4())
            new_budget_row = {
                "id": new_id,
                "ad_campaign_id": campaign_id,
                "daily_budget": new_daily_budget,
                "currency": currency,
                "start_date": eff_date_str,
                "end_date": new_end_date,
                "created_at": now_iso,
            }
            rest_insert("ad_campaign_budgets", new_budget_row)
            return {
                "message": f"Daily budget set to {new_daily_budget} {currency} effective {eff_date_str}",
                "budget": new_budget_row,
            }
        except Exception as exc:
            logger.warning("Supabase budget update failed, using local store: %s", exc)

    # Local store fallback
    store = _load_local_store()
    camp_budgets = store.setdefault("budgets", [])

    if existing_active:
        for b in camp_budgets:
            if str(b.get("id")) == str(existing_active.get("id")):
                existing_start = str(b.get("start_date") or "")[:10]
                if existing_start == eff_date_str:
                    b["daily_budget"] = new_daily_budget
                    b["currency"] = currency
                    _save_local_store(store)
                    return {"message": "Budget updated for effective date", "budget": b}
                else:
                    b["end_date"] = yesterday_str

    new_budget_record = {
        "id": str(uuid.uuid4()),
        "ad_campaign_id": campaign_id,
        "daily_budget": new_daily_budget,
        "currency": currency,
        "start_date": eff_date_str,
        "end_date": new_end_date,
        "created_at": now_iso,
    }
    camp_budgets.append(new_budget_record)
    _save_local_store(store)

    return {
        "message": f"Daily budget set to {new_daily_budget} {currency} effective {eff_date_str}",
        "budget": new_budget_record,
    }


@router.post("/ad-campaigns/{campaign_id}/budgets")
def create_custom_budget_period(
    campaign_id: str,
    payload: CustomBudgetPeriodRequest,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    """Explicitly create a budget entry for any past or custom date period."""
    start_date = payload.start_date.strip()[:10]
    end_date = payload.end_date.strip()[:10] if payload.end_date else None
    now_iso = datetime.now().astimezone().isoformat()
    new_id = str(uuid.uuid4())

    new_budget = {
        "id": new_id,
        "ad_campaign_id": campaign_id,
        "daily_budget": round(float(payload.daily_budget), 2),
        "currency": payload.currency.strip().upper(),
        "start_date": start_date,
        "end_date": end_date,
        "created_at": now_iso,
    }

    if _supabase_ad_tables_exist():
        try:
            rest_insert("ad_campaign_budgets", new_budget)
            return {"budget": new_budget}
        except Exception as exc:
            logger.warning("Supabase insert failed: %s", exc)

    store = _load_local_store()
    store.setdefault("budgets", []).append(new_budget)
    _save_local_store(store)
    return {"budget": new_budget}


@router.put("/ad-campaigns/{campaign_id}/budgets/{budget_id}")
def update_specific_budget_period(
    campaign_id: str,
    budget_id: str,
    payload: CustomBudgetPeriodRequest,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    """Edit any historical or current budget period directly."""
    patch_fields = {
        "daily_budget": round(float(payload.daily_budget), 2),
        "currency": payload.currency.strip().upper(),
        "start_date": payload.start_date.strip()[:10],
        "end_date": payload.end_date.strip()[:10] if payload.end_date else None,
    }

    if _supabase_ad_tables_exist():
        try:
            rest_patch("ad_campaign_budgets", patch_fields, match={"id": budget_id})
            row = rest_get_one("ad_campaign_budgets", params={"id": f"eq.{budget_id}"})
            if row:
                return {"budget": row}
        except Exception as exc:
            logger.warning("Supabase budget patch failed: %s", exc)

    store = _load_local_store()
    for b in store.get("budgets", []):
        if str(b.get("id")) == str(budget_id):
            b.update(patch_fields)
            _save_local_store(store)
            return {"budget": b}

    raise HTTPException(status_code=404, detail="Budget record not found")


@router.delete("/ad-campaigns/{campaign_id}/budgets/{budget_id}")
def delete_specific_budget_period(
    campaign_id: str,
    budget_id: str,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    """Delete an individual historical budget period."""
    if _supabase_ad_tables_exist():
        try:
            rest_delete("ad_campaign_budgets", match={"id": budget_id})
            return {"deleted": True, "id": budget_id}
        except Exception as exc:
            logger.warning("Supabase budget delete failed: %s", exc)

    store = _load_local_store()
    store["budgets"] = [b for b in store.get("budgets", []) if str(b.get("id")) != str(budget_id)]
    _save_local_store(store)
    return {"deleted": True, "id": budget_id}


@router.get("/ad-campaigns/{campaign_id}/budget-history")
def get_budget_history(
    campaign_id: str,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    """Get the full chronological audit history of budget adjustments for a campaign."""
    _campaigns, budgets = _get_campaigns_data()
    history = [b for b in budgets if str(b.get("ad_campaign_id")) == str(campaign_id)]
    history.sort(key=lambda r: str(r.get("start_date") or ""), reverse=True)
    return {"history": history}


@router.delete("/ad-campaigns/{campaign_id}")
def delete_ad_campaign(
    campaign_id: str,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    """Delete an ad campaign and its budget history."""
    if _supabase_ad_tables_exist():
        try:
            rest_delete("ad_campaign_budgets", match={"ad_campaign_id": campaign_id})
            rest_delete("ad_campaigns", match={"id": campaign_id})
            return {"deleted": True, "id": campaign_id}
        except Exception as exc:
            logger.warning("Supabase delete failed: %s", exc)

    store = _load_local_store()
    store["campaigns"] = [c for c in store.get("campaigns", []) if str(c.get("id")) != str(campaign_id)]
    store["budgets"] = [b for b in store.get("budgets", []) if str(b.get("ad_campaign_id")) != str(campaign_id)]
    _save_local_store(store)
    return {"deleted": True, "id": campaign_id}


# ---------------------------------------------------------------------------
# Reporting & Performance Calculation
# ---------------------------------------------------------------------------

def _match_donation_to_campaign(
    donation: dict[str, Any],
    campaign: dict[str, Any],
) -> bool:
    """Determine if a donation is attributed to this ad campaign.

    Matches by:
    1. Exact or substring match on utm_campaign (case-insensitive)
    2. Fallback to channel/source match ONLY if no specific utm_campaign is defined on the campaign
    """
    utm = donation.get("utm")
    if not isinstance(utm, dict):
        return False

    camp_utm = (campaign.get("utm_campaign") or "").strip().lower()
    camp_src = (campaign.get("utm_source") or "").strip().lower()
    camp_channel = str(campaign.get("channel") or "").strip().lower()

    don_campaign = str(utm.get("campaign") or utm.get("utm_campaign") or "").strip().lower()
    don_source = str(utm.get("source") or utm.get("utm_source") or "").strip().lower()

    # 1. If campaign specifies a utm_campaign, it MUST match the donation's campaign tag
    if camp_utm:
        if not don_campaign:
            return False
        return camp_utm == don_campaign or camp_utm in don_campaign or don_campaign in camp_utm

    # 2. For broad channel/source catch-all campaigns (no utm_campaign tag):
    channel_sources = {
        "meta": ("facebook", "fb", "meta", "instagram", "ig"),
        "google": ("google", "adwords", "gads", "youtube"),
        "tiktok": ("tiktok", "tt"),
    }

    # If channel is specified, source MUST match that channel
    if camp_channel in channel_sources:
        valid_sources = channel_sources[camp_channel]
        if don_source in valid_sources:
            return True
        if camp_src and camp_src in valid_sources and (camp_src == don_source or camp_src in don_source):
            return True
        return False

    # Fallback to source match
    if camp_src and don_source and (camp_src == don_source or camp_src in don_source):
        return True

    return False


@router.get("/ad-spend/performance")
def get_ad_spend_performance(
    user: Annotated[AuthUser, Depends(require_super_admin)],
    date_preset: str = Query("7d"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    timezone: str | None = Query(None),
    reporting_currency: str = Query("USD"),
    channel: str = Query("all"),
    attribution_mode: str = Query("attributed"),  # "attributed" vs "blended"
    campaign_id: str | None = Query(None),
) -> dict[str, Any]:
    """Calculate daily ad spend, gross raised, Net Remaining, ROAS, and CPA."""
    preset_val = _param_str(date_preset, "7d") or "7d"
    from_val = _param_str(date_from, "") or None
    to_val = _param_str(date_to, "") or None
    tz_input = _param_str(timezone, _DEFAULT_PLATFORM_TZ) or _DEFAULT_PLATFORM_TZ
    rep_curr = _param_str(reporting_currency, "USD").upper() or "USD"
    chan_filter = _param_str(channel, "all") or "all"
    attr_mode = _param_str(attribution_mode, "attributed") or "attributed"
    camp_filter = _param_str(campaign_id, "all") or "all"

    tz_name = ad._org_zone(tz_input).key
    tz = ad._org_zone(tz_name)

    resolved_from, resolved_to = ad._insights_date_range(
        preset_val, from_val, to_val, tz_name
    )
    date_label = ad._date_label(preset_val, from_val, to_val, tz_name)

    # 1. Load campaigns and budgets
    all_campaigns, all_budgets = _get_campaigns_data()

    filtered_campaigns: list[dict[str, Any]] = []
    for c in all_campaigns:
        if camp_filter and camp_filter != "all" and str(c["id"]) != camp_filter:
            continue
        if chan_filter and chan_filter != "all" and str(c.get("channel")) != chan_filter:
            continue
        filtered_campaigns.append(c)

    # 2. Query donations within the date range
    params: dict[str, str] = {
        "select": "id,amount,currency,created_at,campaign_id,utm,status,payment_method,payment_processor",
        "order": "created_at.asc",
        "limit": "50000",
    }
    if resolved_from and resolved_to:
        params["and"] = f"(created_at.gte.{resolved_from},created_at.lte.{resolved_to})"
    elif resolved_from:
        params["created_at"] = f"gte.{resolved_from}"
    elif resolved_to:
        params["created_at"] = f"lte.{resolved_to}"

    raw_donations = rest_get("donations", params=params) or []
    countable_donations = ad._insights_countable(raw_donations)

    # 3. Determine all calendar days in range
    now_tz = datetime.now(tz)
    today_date = now_tz.date()

    if resolved_from and resolved_to:
        start_dt = datetime.fromisoformat(resolved_from.replace("Z", "+00:00")).astimezone(tz).date()
        end_dt = datetime.fromisoformat(resolved_to.replace("Z", "+00:00")).astimezone(tz).date()
    elif preset_val == "today":
        start_dt = end_dt = today_date
    elif preset_val == "yesterday":
        start_dt = end_dt = today_date - timedelta(days=1)
    elif preset_val == "30d":
        start_dt = today_date - timedelta(days=29)
        end_dt = today_date
    else:  # 7d default
        start_dt = today_date - timedelta(days=6)
        end_dt = today_date

    calendar_days: list[str] = []
    curr = start_dt
    while curr <= end_dt:
        calendar_days.append(curr.strftime("%Y-%m-%d"))
        curr += timedelta(days=1)

    # Group donations by calendar day in the target timezone
    donations_by_day: dict[str, list[dict[str, Any]]] = {d: [] for d in calendar_days}
    for don in countable_donations:
        created_str = don.get("created_at")
        if not created_str:
            continue
        try:
            don_dt = datetime.fromisoformat(str(created_str).replace("Z", "+00:00")).astimezone(tz)
            don_day = don_dt.strftime("%Y-%m-%d")
            if don_day in donations_by_day:
                donations_by_day[don_day].append(don)
        except Exception:
            continue

    # 4. Compute daily breakdown and per-campaign stats (Direct Campaign Expense Model)
    daily_breakdown: list[dict[str, Any]] = []
    campaign_totals: dict[str, dict[str, Any]] = {
        str(c["id"]): {
            "id": str(c["id"]),
            "name": c.get("name"),
            "channel": c.get("channel"),
            "status": c.get("status"),
            "spend": 0.0,
            "daily_budget": 0.0,
        }
        for c in filtered_campaigns
    }

    total_platform_spend = 0.0
    total_platform_gross = 0.0
    all_platform_donors_set: set[str] = set()

    for day_str in calendar_days:
        day_donations = donations_by_day.get(day_str, [])
        day_spend = 0.0
        day_campaign_details: list[dict[str, Any]] = []

        # All platform donations for this day (Gross Raised)
        day_all_donor_ids: set[str] = set()
        day_all_gross = 0.0
        for don in day_donations:
            don_id = str(don.get("id"))
            day_all_donor_ids.add(don_id)
            all_platform_donors_set.add(don_id)
            amt = float(don.get("amount") or 0.0)
            curr = str(don.get("currency") or rep_curr).upper()
            day_all_gross += convert_to_reporting(amt, curr, rep_curr)

        day_gross = round(day_all_gross, 2)
        day_donors_count = len(day_all_donor_ids)

        for camp in filtered_campaigns:
            camp_id = str(camp["id"])
            eff_budget_rec = _find_effective_budget_on_date(all_budgets, camp_id, day_str)
            raw_daily_budget = float(eff_budget_rec.get("daily_budget") or 0.0) if eff_budget_rec else 0.0
            budget_curr = str(eff_budget_rec.get("currency") or "USD") if eff_budget_rec else "USD"

            # Convert campaign daily budget/expense to reporting currency
            budget_in_rep = convert_to_reporting(raw_daily_budget, budget_curr, rep_curr)
            day_spend += budget_in_rep
            campaign_totals[camp_id]["spend"] += budget_in_rep
            campaign_totals[camp_id]["daily_budget"] = budget_in_rep

            day_campaign_details.append({
                "campaign_id": camp_id,
                "campaign_name": camp.get("name"),
                "channel": camp.get("channel"),
                "daily_budget": budget_in_rep,
                "is_organic": False,
            })

        day_spend = round(day_spend, 2)
        day_net = round(day_gross - day_spend, 2)
        day_roas = round(day_gross / day_spend, 2) if day_spend > 0 else (round(day_gross, 2) if day_gross > 0 else 0.0)
        day_cpa = round(day_spend / day_donors_count, 2) if day_donors_count > 0 else 0.0

        total_platform_spend += day_spend
        total_platform_gross += day_gross

        daily_breakdown.append({
            "date": day_str,
            "spend": day_spend,
            "gross": day_gross,
            "net": day_net,
            "donors": day_donors_count,
            "roas": day_roas,
            "cpa": day_cpa,
            "campaigns_active": len([c for c in filtered_campaigns if str(c.get("status")) == "active"]),
            "details": day_campaign_details,
            "platform_total_donors": day_donors_count,
            "platform_total_gross": day_gross,
            "attributed_donors": day_donors_count,
            "attributed_gross": day_gross,
            "organic_donors": 0,
            "organic_gross": 0.0,
        })

    # Finalize campaign totals
    for ct in campaign_totals.values():
        c_spend = ct["spend"]
        ct["spend"] = round(c_spend, 2)

    total_platform_gross = round(total_platform_gross, 2)
    total_platform_spend = round(total_platform_spend, 2)
    total_platform_net = round(total_platform_gross - total_platform_spend, 2)
    total_donors_count = len(all_platform_donors_set)
    overall_roas = round(total_platform_gross / total_platform_spend, 2) if total_platform_spend > 0 else 0.0
    overall_cpa = round(total_platform_spend / total_donors_count, 2) if total_donors_count > 0 else 0.0

    return {
        "summary": {
            "total_spend": total_platform_spend,
            "total_gross": total_platform_gross,
            "total_net": total_platform_net,
            "roas": overall_roas,
            "cpa": overall_cpa,
            "total_donors": total_donors_count,
            "active_campaigns_count": len([c for c in filtered_campaigns if str(c.get("status")) == "active"]),
            "reporting_currency": rep_curr,
            "date_preset": preset_val,
            "date_label": date_label,
            "attribution_mode": "expense_deduction",
            "platform_total_donors": total_donors_count,
            "platform_total_gross": total_platform_gross,
            "attributed_donors": total_donors_count,
            "attributed_gross": total_platform_gross,
            "organic_donors": 0,
            "organic_gross": 0.0,
        },
        "daily_breakdown": daily_breakdown,
        "campaign_summaries": list(campaign_totals.values()),
        "filter_options": {
            "channels": ["all", "google", "meta", "tiktok", "other"],
            "campaigns": [{"id": str(c["id"]), "name": c.get("name"), "channel": c.get("channel")} for c in all_campaigns],
        },
    }


@router.get("/ad-spend/export-csv")
def export_ad_spend_csv(
    user: Annotated[AuthUser, Depends(require_super_admin)],
    date_preset: str = Query("7d"),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    timezone: str | None = Query(None),
    reporting_currency: str = Query("USD"),
    channel: str = Query("all"),
    attribution_mode: str = Query("attributed"),
    campaign_id: str | None = Query(None),
    format: str | None = Query(None),
) -> Any:
    """Download performance report as CSV (returns JSON with csv text for adminFetch, or raw file)."""
    report = get_ad_spend_performance(
        user=user,
        date_preset=_param_str(date_preset, "7d") or "7d",
        date_from=_param_str(date_from, "") or None,
        date_to=_param_str(date_to, "") or None,
        timezone=_param_str(timezone, _DEFAULT_PLATFORM_TZ) or _DEFAULT_PLATFORM_TZ,
        reporting_currency=_param_str(reporting_currency, "USD") or "USD",
        channel=_param_str(channel, "all") or "all",
        attribution_mode=_param_str(attribution_mode, "attributed") or "attributed",
        campaign_id=_param_str(campaign_id, "all") or "all",
    )

    currency = report["summary"]["reporting_currency"]
    output = io.StringIO()
    writer = csv.writer(output)

    # Header section
    writer.writerow(["Ad Spend & ROI Performance Report"])
    writer.writerow(["Period", report["summary"]["date_label"]])
    writer.writerow(["Reporting Currency", currency])
    writer.writerow(["Total Ad Expense", f"{report['summary']['total_spend']:.2f} {currency}"])
    writer.writerow(["Total Donations Raised", f"{report['summary']['total_gross']:.2f} {currency}"])
    writer.writerow(["Net Remaining", f"{report['summary']['total_net']:.2f} {currency}"])
    writer.writerow(["Overall ROAS", f"{report['summary']['roas']}x"])
    writer.writerow(["Average CPA", f"{report['summary']['cpa']:.2f} {currency}"])
    writer.writerow(["Total Donors", report["summary"]["total_donors"]])
    writer.writerow([])

    # Table columns
    writer.writerow([
        "Date",
        f"Daily Ad Expense ({currency})",
        f"Donations Raised ({currency})",
        f"Net Remaining ({currency})",
        "Total Donors",
        "ROAS",
        f"CPA ({currency})",
        "Active Campaigns",
    ])

    for row in report.get("daily_breakdown", []):
        camp_names = ", ".join(d.get("campaign_name", "") for d in row.get("details", []) if not d.get("is_organic")) or "None"
        writer.writerow([
            row["date"],
            f"{row['spend']:.2f}",
            f"{row['gross']:.2f}",
            f"{row['net']:.2f}",
            row["donors"],
            f"{row['roas']}x",
            f"{row['cpa']:.2f}",
            camp_names,
        ])

    csv_data = output.getvalue()
    today_str = datetime.now().strftime("%Y%m%d")
    filename = f"ad_spend_roi_{today_str}.csv"

    if _param_str(format, "") == "raw":
        return Response(
            content=csv_data,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return {
        "csv": csv_data,
        "filename": filename,
    }
