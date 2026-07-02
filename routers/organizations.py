from __future__ import annotations

import os
import re
import secrets
from typing import Annotated, Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import AuthUser, require_auth, require_org_access
from db import rest_get, rest_get_one, rest_insert, rest_insert_error, rest_patch, select_columns

router = APIRouter(prefix="/orgs", tags=["organizations"])

ROOT_CAMPAIGN_ID = os.getenv("ROOT_CAMPAIGN_ID", "00000000-0000-4000-8000-000000000002")


def _default_campaign_content(name: str) -> dict[str, Any]:
    root = rest_get_one("campaign_content", params={"campaign_id": f"eq.{ROOT_CAMPAIGN_ID}", "select": "*"})
    if root:
        return {
            "title": name,
            "caption": root.get("caption") or "",
            "body_html": root.get("body_html") or "<p>Campaign content goes here.</p>",
            "dedication_hint": root.get("dedication_hint"),
            "primary_color": root.get("primary_color") or "#3872DC",
            "logo_url": root.get("logo_url"),
            "logo_width": root.get("logo_width") or 160,
            "logo_height": root.get("logo_height") or 56,
            "hero_url": root.get("hero_url"),
            "hero_width": root.get("hero_width") or 1248,
            "hero_height": root.get("hero_height") or 702,
            "hero_alt": root.get("hero_alt"),
            "favicon_url": root.get("favicon_url"),
        }
    return {
        "title": name,
        "caption": "",
        "body_html": "<p>Campaign content goes here.</p>",
        "primary_color": "#3872DC",
    }


def _cname_target() -> str:
    explicit = os.getenv("CUSTOM_DOMAIN_CNAME_TARGET", "").strip()
    if explicit:
        return explicit.rstrip(".")
    frontend = os.getenv("FRONTEND_URL", "").strip()
    if ".up.railway.app" in frontend:
        host = urlparse(frontend if "://" in frontend else f"https://{frontend}").hostname
        if host and host.endswith(".up.railway.app"):
            return host
    return ""


def _dns_lookup(name: str, record_type: str) -> list[str]:
    try:
        response = httpx.get(
            "https://dns.google/resolve",
            params={"name": name, "type": record_type},
            timeout=10.0,
        )
        response.raise_for_status()
        answers = response.json().get("Answer") or []
        values: list[str] = []
        for answer in answers:
            data = answer.get("data", "")
            if record_type == "TXT":
                values.append(data.strip('"'))
            elif record_type == "CNAME":
                values.append(data.rstrip("."))
        return values
    except Exception:
        return []


def build_dns_instructions(hostname: str, verification_token: str) -> dict[str, str]:
    target = _cname_target()
    instructions: dict[str, str] = {
        "type": "CNAME",
        "name": hostname,
        "txt_name": hostname,
        "txt_verification": f"uz-verify={verification_token}",
        "note": (
            "Also add the TXT record shown in Railway when you attach this domain there. "
            "Root domains may need ALIAS/CNAME flattening at your DNS provider."
        ),
    }
    if target:
        instructions["value"] = target
    else:
        instructions["value"] = ""
        instructions["cname_missing"] = (
            "Set CUSTOM_DOMAIN_CNAME_TARGET in backend env to your Railway hostname "
            "(e.g. my-app-production-xxxx.up.railway.app from Railway → Settings → Domains)."
        )
    return instructions


def _attach_dns_instructions(domains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for domain in domains:
        row = dict(domain)
        token = str(row.get("verification_token") or "")
        hostname = str(row.get("hostname") or "")
        if hostname and token:
            row["dns_instructions"] = build_dns_instructions(hostname, token)
        enriched.append(row)
    return enriched


class CampaignContentPayload(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    caption: str | None = None
    body_html: str = ""
    dedication_hint: str | None = None
    primary_color: str = "#3872DC"
    logo_url: str | None = None
    logo_width: int = 160
    logo_height: int = 56
    hero_url: str | None = None
    hero_width: int = 1248
    hero_height: int = 702
    hero_alt: str | None = None
    favicon_url: str | None = None


class CreateCampaignRequest(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    slug: str | None = None
    default_currency: str = "USD"


class UpdateCampaignRequest(BaseModel):
    name: str | None = None
    status: str | None = None
    default_currency: str | None = None
    stripe_account_id: str | None = None
    content: CampaignContentPayload | None = None


class CurrencyConfig(BaseModel):
    currency_code: str
    enabled: bool = True
    is_default: bool = False
    amounts_once: list[dict[str, Any]] | None = None
    amounts_monthly: list[dict[str, Any]] | None = None


class DomainRequest(BaseModel):
    hostname: str = Field(min_length=4, max_length=253)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or secrets.token_hex(4)


def _get_org_id(user: AuthUser, org_id: str) -> str:
    require_org_access(org_id, user, min_role="member")
    return org_id


@router.get("/{org_id}/campaigns")
def list_campaigns(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> list[dict[str, Any]]:
    _get_org_id(user, org_id)
    return rest_get(
        "campaigns",
        params={
            "organization_id": f"eq.{org_id}",
            "select": select_columns("id", "name", "slug", "status", "default_currency", "created_at", "updated_at"),
            "order": "created_at.desc",
        },
    )


@router.post("/{org_id}/campaigns")
def create_campaign(
    org_id: str,
    payload: CreateCampaignRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    slug = payload.slug or _slugify(payload.name)
    existing = rest_get_one(
        "campaigns",
        params={"organization_id": f"eq.{org_id}", "slug": f"eq.{slug}", "select": "id"},
    )
    if existing:
        raise HTTPException(status_code=400, detail=f"Campaign slug '{slug}' already exists in this organization")

    campaign = rest_insert(
        "campaigns",
        {
            "organization_id": org_id,
            "name": payload.name,
            "slug": slug,
            "default_currency": payload.default_currency.upper(),
            "status": "draft",
        },
    )
    if not campaign:
        err = rest_insert_error(
            "campaigns",
            {
                "organization_id": org_id,
                "name": payload.name,
                "slug": slug,
                "default_currency": payload.default_currency.upper(),
                "status": "draft",
            },
        )
        raise HTTPException(status_code=500, detail=err or "Failed to create campaign")
    rest_insert(
        "campaign_content",
        {"campaign_id": campaign["id"], **_default_campaign_content(payload.name)},
    )
    return campaign


@router.get("/{org_id}/campaigns/{campaign_id}")
def get_campaign(
    org_id: str,
    campaign_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    _get_org_id(user, org_id)
    campaign = rest_get_one(
        "campaigns",
        params={
            "id": f"eq.{campaign_id}",
            "organization_id": f"eq.{org_id}",
            "select": "*",
        },
    )
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    content = rest_get_one(
        "campaign_content",
        params={"campaign_id": f"eq.{campaign_id}", "select": "*"},
    )
    currencies = rest_get(
        "campaign_currencies",
        params={"campaign_id": f"eq.{campaign_id}", "select": "*"},
    )
    domains = _attach_dns_instructions(
        rest_get(
            "domains",
            params={"campaign_id": f"eq.{campaign_id}", "select": "*"},
        )
    )
    stripe_accounts = rest_get(
        "stripe_accounts",
        params={"organization_id": f"eq.{org_id}", "select": "*"},
    )
    return {
        "campaign": campaign,
        "content": content,
        "currencies": currencies,
        "domains": domains,
        "stripe_accounts": stripe_accounts,
    }


@router.patch("/{org_id}/campaigns/{campaign_id}")
def update_campaign(
    org_id: str,
    campaign_id: str,
    payload: UpdateCampaignRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    updates: dict[str, Any] = {}
    if payload.name is not None:
        updates["name"] = payload.name
    if payload.status is not None:
        updates["status"] = payload.status
    if payload.default_currency is not None:
        updates["default_currency"] = payload.default_currency.upper()
    if payload.stripe_account_id is not None:
        updates["stripe_account_id"] = payload.stripe_account_id or None

    campaign = None
    if updates:
        campaign = rest_patch("campaigns", updates, match={"id": campaign_id})

    if payload.content:
        content_data = payload.content.model_dump()
        if content_data.get("logo_url") and (content_data.get("logo_width") != 160 or content_data.get("logo_height") != 56):
            raise HTTPException(status_code=400, detail="Logo must be 160×56 pixels")
        if content_data.get("hero_url") and (content_data.get("hero_width") != 1248 or content_data.get("hero_height") != 702):
            raise HTTPException(status_code=400, detail="Hero image must be 1248×702 pixels")
        existing = rest_get_one("campaign_content", params={"campaign_id": f"eq.{campaign_id}", "select": "campaign_id"})
        if existing:
            rest_patch("campaign_content", content_data, match={"campaign_id": campaign_id})
        else:
            rest_insert("campaign_content", {"campaign_id": campaign_id, **content_data})

    return get_campaign(org_id, campaign_id, user)


@router.put("/{org_id}/campaigns/{campaign_id}/currencies")
def update_currencies(
    org_id: str,
    campaign_id: str,
    currencies: list[CurrencyConfig],
    user: Annotated[AuthUser, Depends(require_auth)],
) -> list[dict[str, Any]]:
    require_org_access(org_id, user, min_role="admin")
    results = []
    for c in currencies:
        existing = rest_get_one(
            "campaign_currencies",
            params={"campaign_id": f"eq.{campaign_id}", "currency_code": f"eq.{c.currency_code.upper()}"},
        )
        row = {
            "campaign_id": campaign_id,
            "currency_code": c.currency_code.upper(),
            "enabled": c.enabled,
            "is_default": c.is_default,
            "amounts_once": c.amounts_once,
            "amounts_monthly": c.amounts_monthly,
        }
        if existing:
            saved = rest_patch("campaign_currencies", row, match={"id": existing["id"]})
        else:
            saved = rest_insert("campaign_currencies", row)
        if saved:
            results.append(saved)
    return results


@router.post("/{org_id}/campaigns/{campaign_id}/domains")
def add_domain(
    org_id: str,
    campaign_id: str,
    payload: DomainRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    hostname = payload.hostname.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
    domain = rest_insert(
        "domains",
        {"campaign_id": campaign_id, "hostname": hostname},
    )
    if not domain:
        raise HTTPException(status_code=400, detail="Domain already exists or invalid")
    token = str(domain.get("verification_token") or "")
    return {
        **domain,
        "dns_instructions": build_dns_instructions(hostname, token),
    }


@router.get("/{org_id}/campaigns/{campaign_id}/domains/{domain_id}/dns")
def get_domain_dns(
    org_id: str,
    campaign_id: str,
    domain_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    domain = rest_get_one(
        "domains",
        params={"id": f"eq.{domain_id}", "campaign_id": f"eq.{campaign_id}", "select": "*"},
    )
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    hostname = str(domain.get("hostname") or "")
    token = str(domain.get("verification_token") or "")
    return {"dns_instructions": build_dns_instructions(hostname, token)}


@router.get("/{org_id}/campaigns/{campaign_id}/domains/{domain_id}/status")
def domain_dns_status(
    org_id: str,
    campaign_id: str,
    domain_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    domain = rest_get_one(
        "domains",
        params={"id": f"eq.{domain_id}", "campaign_id": f"eq.{campaign_id}", "select": "*"},
    )
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    hostname = str(domain.get("hostname") or "")
    token = str(domain.get("verification_token") or "")
    expected_txt = f"uz-verify={token}"
    txt_records = _dns_lookup(hostname, "TXT")
    txt_ok = expected_txt in txt_records or any(expected_txt in r for r in txt_records)
    target = _cname_target()
    cname_records = _dns_lookup(hostname, "CNAME") if target else []
    cname_ok = not target or any(r == target for r in cname_records)
    return {
        "hostname": hostname,
        "verified_at": domain.get("verified_at"),
        "txt_ok": txt_ok,
        "cname_ok": cname_ok,
        "cname_target": target,
        "cname_found": cname_records,
        "txt_expected": expected_txt,
        "ready": bool(txt_ok and cname_ok),
    }


@router.post("/{org_id}/campaigns/{campaign_id}/domains/{domain_id}/verify")
def verify_domain(
    org_id: str,
    campaign_id: str,
    domain_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    domain = rest_get_one(
        "domains",
        params={"id": f"eq.{domain_id}", "campaign_id": f"eq.{campaign_id}", "select": "*"},
    )
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    hostname = str(domain.get("hostname") or "")
    token = str(domain.get("verification_token") or "")
    expected_txt = f"uz-verify={token}"
    txt_records = _dns_lookup(hostname, "TXT")
    if expected_txt not in txt_records and not any(expected_txt in record for record in txt_records):
        raise HTTPException(
            status_code=400,
            detail=f"TXT record not found for {hostname}. Add TXT: {expected_txt}",
        )
    target = _cname_target()
    if target:
        cname_records = _dns_lookup(hostname, "CNAME")
        if cname_records and not any(record == target for record in cname_records):
            raise HTTPException(
                status_code=400,
                detail=f"CNAME for {hostname} must point to {target} (found: {', '.join(cname_records) or 'none'})",
            )
    from datetime import datetime, timezone
    updated = rest_patch(
        "domains",
        {"verified_at": datetime.now(timezone.utc).isoformat(), "ssl_status": "active"},
        match={"id": domain_id, "campaign_id": campaign_id},
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Domain not found")
    return updated


@router.get("/{org_id}/members")
def list_members(org_id: str, user: Annotated[AuthUser, Depends(require_auth)]) -> list[dict[str, Any]]:
    _get_org_id(user, org_id)
    return rest_get(
        "organization_members",
        params={
            "organization_id": f"eq.{org_id}",
            "select": "id,user_id,role,created_at,profiles(id,first_name,last_name,role)",
        },
    )


class TeamInviteRequest(BaseModel):
    email: str = Field(min_length=3)
    role: str = "admin"


@router.post("/{org_id}/invites")
def invite_member(
    org_id: str,
    payload: TeamInviteRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    invite = rest_insert(
        "organization_invites",
        {
            "organization_id": org_id,
            "email": payload.email.lower(),
            "role": payload.role,
            "invited_by": user.id,
        },
    )
    if not invite:
        raise HTTPException(status_code=400, detail="Failed to create invite")
    return {"invite": invite, "invite_url": f"/invite/{invite['token']}"}


@router.get("/{org_id}/settings")
def get_org_settings(org_id: str, user: Annotated[AuthUser, Depends(require_auth)]) -> dict[str, Any]:
    _get_org_id(user, org_id)
    org = rest_get_one(
        "organizations",
        params={"id": f"eq.{org_id}", "select": "id,name,default_currency,reporting_currency,payment_methods,notification_prefs"},
    )
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


@router.patch("/{org_id}/settings")
def update_org_settings(
    org_id: str,
    payload: dict[str, Any],
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    allowed = {"name", "default_currency", "reporting_currency", "payment_methods", "notification_prefs"}
    updates = {k: v for k, v in payload.items() if k in allowed}
    updated = rest_patch("organizations", updates, match={"id": org_id})
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update organization")
    return updated
