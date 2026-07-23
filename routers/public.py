from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from db import rest_get, rest_get_one, rest_insert, select_columns
from site_constants import ROOT_CAMPAIGN_ID

router = APIRouter(prefix="/public", tags=["public"])


def _load_campaign_bundle(campaign: dict[str, Any]) -> dict[str, Any]:
    campaign_id = campaign["id"]
    content = rest_get_one("campaign_content", params={"campaign_id": f"eq.{campaign_id}", "select": "*"})
    currencies = rest_get("campaign_currencies", params={"campaign_id": f"eq.{campaign_id}", "select": "*"})
    org = rest_get_one(
        "organizations",
        params={"id": f"eq.{campaign['organization_id']}", "select": "id,name,slug,default_currency,reporting_currency,payment_methods"},
    )
    return {
        "campaign": campaign,
        "content": content,
        "currencies": currencies,
        "organization": org,
    }


@router.get("/root-campaign")
def get_root_campaign() -> dict[str, Any]:
    campaign = rest_get_one(
        "campaigns",
        params={"id": f"eq.{ROOT_CAMPAIGN_ID}", "status": "eq.live", "select": "*"},
    )
    if not campaign:
        raise HTTPException(status_code=404, detail="Root campaign not found or not live")
    return _load_campaign_bundle(campaign)


@router.get("/campaign")
def get_campaign_by_slug_or_host(
    slug: str | None = Query(None),
    host: str | None = Query(None),
    org_id: str | None = Query(None),
    campaign_id: str | None = Query(None),
) -> dict[str, Any]:
    campaign = None
    if org_id and campaign_id:
        campaign = rest_get_one(
            "campaigns",
            params={
                "id": f"eq.{campaign_id}",
                "organization_id": f"eq.{org_id}",
                "select": "*",
            },
        )
    if host:
        host_clean = host.lower().split(":")[0]
        domain = rest_get_one("domains", params={"hostname": f"eq.{host_clean}", "select": "campaign_id,verified_at"})
        if domain and domain.get("verified_at"):
            campaign = rest_get_one("campaigns", params={"id": f"eq.{domain['campaign_id']}", "status": "eq.live", "select": "*"})

    if not campaign and slug:
        campaign = rest_get_one("campaigns", params={"slug": f"eq.{slug}", "status": "eq.live", "select": "*"})

    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    return _load_campaign_bundle(campaign)


@router.get("/campaigns/{campaign_id}/donations")
def get_campaign_donations(
    campaign_id: str,
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    rows = rest_get(
        "donations",
        params={
            "campaign_id": f"eq.{campaign_id}",
            "select": select_columns("id", "first_name", "last_name", "amount", "currency", "frequency", "honoree_name", "created_at", "device", "crypto_amount", "crypto_currency"),
            "order": "created_at.desc",
            "limit": str(limit + 1),
            "offset": str(offset),
        },
    )
    has_more = len(rows) > limit
    return {"donations": rows[:limit], "has_more": has_more}


@router.get("/resolve-host")
def resolve_host(host: str = Query(...)) -> dict[str, Any]:
    host_clean = host.lower().split(":")[0]
    domain = rest_get_one("domains", params={"hostname": f"eq.{host_clean}", "select": "campaign_id,verified_at"})
    if not domain or not domain.get("verified_at"):
        return {"campaign_id": None, "slug": None}
    campaign = rest_get_one("campaigns", params={"id": f"eq.{domain['campaign_id']}", "select": "id,slug,status,organization_id"})
    if not campaign or campaign.get("status") != "live":
        return {"campaign_id": None, "organization_id": None, "slug": None}
    return {
        "campaign_id": campaign["id"],
        "organization_id": campaign.get("organization_id"),
        "slug": campaign["slug"],
    }


@router.post("/problem-reports")
def create_problem_report(payload: dict[str, Any]) -> dict[str, Any]:
    description = str(payload.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="Description is required")
    if len(description) > 500:
        description = description[:500]

    organization_id = payload.get("organization_id") or None
    campaign_id = payload.get("campaign_id") or None
    if organization_id:
        organization_id = str(organization_id).strip() or None
    if campaign_id:
        campaign_id = str(campaign_id).strip() or None

    if campaign_id and not organization_id:
        campaign = rest_get_one(
            "campaigns",
            params={"id": f"eq.{campaign_id}", "select": "id,organization_id"},
        )
        if campaign:
            organization_id = campaign.get("organization_id")

    row = rest_insert(
        "problem_reports",
        {
            "organization_id": organization_id,
            "campaign_id": campaign_id,
            "description": description,
        },
    )
    if not row:
        raise HTTPException(status_code=400, detail="Unable to save report")
    return {"ok": True, "id": row.get("id")}


class _TranslateTexts(BaseModel):
    title: str = ""
    titleHtml: str = ""
    titleHtmlMobile: str = ""
    caption: str = ""
    captionMobile: str = ""
    bodyHtml: str = ""
    bodyHtmlMobile: str = ""
    dedicationHint: str = ""
    landingHeadlineHtml: str = ""
    landingBodyHtml: str = ""
    modalTitle: str = ""
    modalTitleHtml: str = ""
    modalBodyHtml: str = ""
    modalTitleMobile: str = ""
    modalTitleHtmlMobile: str = ""
    modalBodyHtmlMobile: str = ""


class TranslateCampaignBody(BaseModel):
    campaign_id: str = Field(min_length=1, max_length=80)
    target_language: str = Field(min_length=2, max_length=16)
    language_name: str | None = Field(default=None, max_length=80)
    texts: _TranslateTexts
    ui_strings: dict[str, str] | None = None


@router.post("/translate-campaign")
def translate_campaign(payload: TranslateCampaignBody) -> dict[str, Any]:
    from routers.ai_content import localize_campaign_texts

    texts = payload.texts.model_dump()
    ui_in = {str(k): str(v) for k, v in (payload.ui_strings or {}).items() if str(v).strip()}
    merged: dict[str, str] = {}
    for key, value in texts.items():
        merged[f"c__{key}"] = str(value or "")
    for key, value in ui_in.items():
        merged[f"u__{key}"] = value

    localized_merged = localize_campaign_texts(
        payload.target_language,
        merged,
        language_name=payload.language_name,
    )

    localized: dict[str, str] = {}
    ui_out: dict[str, str] = {}
    for key, value in localized_merged.items():
        if key.startswith("c__"):
            localized[key[3:]] = str(value)
        elif key.startswith("u__"):
            ui_out[key[3:]] = str(value)

    # Preserve any English UI keys the model dropped.
    for key, value in ui_in.items():
        ui_out.setdefault(key, value)
    for key, value in texts.items():
        localized.setdefault(key, str(value or ""))

    return {
        "campaign_id": payload.campaign_id,
        "target_language": payload.target_language,
        "texts": localized,
        "ui_strings": ui_out,
    }
