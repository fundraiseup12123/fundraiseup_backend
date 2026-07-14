from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from db import rest_get, rest_get_one, select_columns
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
