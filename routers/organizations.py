from __future__ import annotations

import os
import re
import secrets
from typing import Annotated, Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from auth import AuthUser, deny_platform_admin_payment_writes, require_auth, require_org_access
from db import rest_delete, rest_get, rest_get_one, rest_insert, rest_insert_error, rest_patch, select_columns
from routers.payment_accounts import normalize_payment_account_sources
from invite_service import send_pending_organization_invite
from domain_utils import (
    platform_domain_config,
    platform_root_domain,
    resolve_campaign_hostname,
    subdomain_label_from_hostname,
)

router = APIRouter(prefix="/orgs", tags=["organizations"])

ROOT_CAMPAIGN_ID = os.getenv("ROOT_CAMPAIGN_ID", "00000000-0000-4000-8000-000000000002")

# Same once/monthly tiers as hope-for-gaza (USD) and frontend currency.ts defaults.
_USD_ONCE_PRESETS = [1170, 500, 250, 115, 50, 25]
_USD_MONTHLY_PRESETS = [95, 45, 25, 15, 10, 5]
_USD_TO_LOCAL = {
    "USD": 1.0,
    "GBP": 0.79,
    "EUR": 0.92,
    "AUD": 1.55,
    "CAD": 1.36,
    "CHF": 0.88,
    "NZD": 1.68,
    "PKR": 278.0,
    "INR": 83.5,
    "AED": 3.67,
    "SAR": 3.75,
}


def _round_ending_0_or_5(value: float) -> int:
    if value <= 0 or value != value:  # NaN check
        return 5
    lower = int(value // 5) * 5
    rem = value - lower
    if rem == 0:
        return max(5, lower)
    if rem >= 2:
        return max(5, lower + 5)
    return max(5, lower)


def _round_preset_amount(amount: float, currency: str) -> int:
    code = currency.upper()
    if code == "PKR" and amount >= 1000:
        thousands = max(1, _round_ending_0_or_5(amount / 1000.0))
        return int(thousands * 1000)
    return _round_ending_0_or_5(amount)


def _format_preset_label(value: int, currency: str) -> str:
    code = currency.upper()
    if code == "PKR":
        if value >= 1000:
            return f"Rs {int(round(value / 1000.0))}K"
        return f"Rs {value:,}"
    if code == "USD":
        return f"${value:,}"
    return f"{value:,}"


def _default_amount_presets(currency: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    code = (currency or "USD").strip().upper() or "USD"
    if code == "PKR":
        once_vals = [700000, 300000, 150000, 70000, 30000, 15000]
        monthly_vals = [56000, 28000, 14000, 8400, 2800, 1400]
    elif code == "USD":
        once_vals = list(_USD_ONCE_PRESETS)
        monthly_vals = list(_USD_MONTHLY_PRESETS)
    else:
        rate = float(_USD_TO_LOCAL.get(code, 1.0))
        once_vals = [_round_preset_amount(v * rate, code) for v in _USD_ONCE_PRESETS]
        monthly_vals = [_round_preset_amount(v * rate, code) for v in _USD_MONTHLY_PRESETS]

    once = [{"label": _format_preset_label(v, code), "value": v} for v in once_vals]
    monthly = [{"label": _format_preset_label(v, code), "value": v} for v in monthly_vals]
    return once, monthly


def _seed_default_campaign_currencies(campaign_id: str, default_currency: str) -> None:
    """Attach hope-for-gaza-style once/monthly presets for the campaign default currency."""
    code = (default_currency or "USD").strip().upper() or "USD"
    once, monthly = _default_amount_presets(code)
    rest_insert(
        "campaign_currencies",
        {
            "campaign_id": campaign_id,
            "currency_code": code,
            "enabled": True,
            "is_default": True,
            "amounts_once": once,
            "amounts_monthly": monthly,
        },
    )
    # Match hope-for-gaza: also keep USD presets available when default is not USD.
    if code != "USD":
        usd_once, usd_monthly = _default_amount_presets("USD")
        rest_insert(
            "campaign_currencies",
            {
                "campaign_id": campaign_id,
                "currency_code": "USD",
                "enabled": True,
                "is_default": False,
                "amounts_once": usd_once,
                "amounts_monthly": usd_monthly,
            },
        )


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
            "logo_width": root.get("logo_width") or 80,
            "logo_height": root.get("logo_height") or 80,
            "hero_url": root.get("hero_url"),
            "hero_width": root.get("hero_width") or 1248,
            "hero_height": root.get("hero_height") or 702,
            "hero_alt": root.get("hero_alt"),
            "favicon_url": root.get("favicon_url"),
            "show_donor_country": bool(root.get("show_donor_country") or False),
            "recent_donations_sort": (
                root.get("recent_donations_sort")
                if root.get("recent_donations_sort") in {"recent", "descending"}
                else "recent"
            ),
            "ga4_measurement_id": root.get("ga4_measurement_id") or None,
            "gtm_container_id": root.get("gtm_container_id") or None,
            "ga4_property_id": root.get("ga4_property_id") or None,
            "meta_pixel_id": root.get("meta_pixel_id") or None,
            "title_html": None,
            "title_font_size": root.get("title_font_size"),
            "body_font_size": root.get("body_font_size"),
        }
    return {
        "title": name,
        "caption": "",
        "body_html": "<p>Campaign content goes here.</p>",
        "primary_color": "#3872DC",
        "show_donor_country": False,
        "ga4_measurement_id": None,
        "gtm_container_id": None,
        "ga4_property_id": None,
        "meta_pixel_id": None,
        "title_html": None,
        "title_font_size": None,
        "body_font_size": None,
        "recent_donations_sort": "recent",
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


def build_dns_instructions(hostname: str, verification_token: str, *, auto_configured: bool = False) -> dict[str, str]:
    root = platform_root_domain()
    if auto_configured and root:
        target = _cname_target()
        return {
            "type": "platform_subdomain",
            "name": hostname,
            "value": target,
            "note": (
                f"This subdomain is configured automatically as {hostname}. "
                f"Ensure wildcard DNS *.{root} points to your hosting"
                + (f" ({target})." if target else ".")
            ),
        }

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


def _enrich_domain_row(domain: dict[str, Any], campaigns_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    row = dict(domain)
    token = str(row.get("verification_token") or "")
    hostname = str(row.get("hostname") or "")
    campaign_id = str(row.get("campaign_id") or "")
    campaign = campaigns_by_id.get(campaign_id) or {}
    row["campaign_name"] = str(campaign.get("name") or "Unknown campaign")
    row["campaign_status"] = str(campaign.get("status") or "")
    row["subdomain_label"] = subdomain_label_from_hostname(hostname) or hostname.split(".")[0]
    is_platform = bool(hostname and row.get("verified_at") and subdomain_label_from_hostname(hostname))
    if hostname and token:
        row["dns_instructions"] = build_dns_instructions(
            hostname,
            token,
            auto_configured=is_platform,
        )
    return row


def _attach_dns_instructions(domains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not domains:
        return []
    campaign_ids = list({str(d.get("campaign_id") or "") for d in domains if d.get("campaign_id")})
    campaigns_by_id: dict[str, dict[str, Any]] = {}
    for campaign_id in campaign_ids:
        campaign = rest_get_one("campaigns", params={"id": f"eq.{campaign_id}", "select": "id,name,status"})
        if campaign:
            campaigns_by_id[campaign_id] = campaign
    return [_enrich_domain_row(domain, campaigns_by_id) for domain in domains]


class PopupViewPayload(BaseModel):
    logo_url: str | None = None
    logo_width: int = 200
    logo_height: int = 63
    hero_url: str | None = None
    hero_width: int = 750
    hero_height: int = 430
    hero_alt: str | None = None
    landing_headline_html: str = ""
    landing_body_html: str = ""
    modal_title: str = ""
    modal_title_html: str | None = None
    modal_body_html: str = ""
    modal_title_font_size: int | None = None
    modal_body_font_size: int | None = None

    @field_validator("modal_title_font_size", "modal_body_font_size", mode="before")
    @classmethod
    def _normalize_popup_font_size(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            size = int(round(float(value)))
        except (TypeError, ValueError):
            return None
        if size < 10 or size > 72:
            raise ValueError("Font size must be between 10 and 72")
        return size


class CampaignContentPayload(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    caption: str | None = None
    body_html: str = ""
    dedication_hint: str | None = None
    primary_color: str = "#3872DC"
    logo_url: str | None = None
    logo_width: int = 80
    logo_height: int = 80
    hero_url: str | None = None
    hero_width: int = 1248
    hero_height: int = 702
    hero_alt: str | None = None
    favicon_url: str | None = None
    popup_view_json: str | None = None
    show_donor_country: bool = False
    recent_donations_sort: str = Field(default="recent", pattern="^(recent|descending)$")
    ga4_measurement_id: str | None = None
    gtm_container_id: str | None = None
    ga4_property_id: str | None = None
    meta_pixel_id: str | None = None
    title_html: str | None = None
    title_font_size: int | None = None
    body_font_size: int | None = None
    title_html_mobile: str | None = None
    body_html_mobile: str | None = None
    caption_mobile: str | None = None
    title_font_size_mobile: int | None = None
    body_font_size_mobile: int | None = None

    @field_validator(
        "title_font_size",
        "body_font_size",
        "title_font_size_mobile",
        "body_font_size_mobile",
        mode="before",
    )
    @classmethod
    def _normalize_landing_font_size(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            size = int(round(float(value)))
        except (TypeError, ValueError):
            return None
        if size < 10 or size > 72:
            raise ValueError("Font size must be between 10 and 72")
        return size

    def normalize_analytics_ids(self) -> "CampaignContentPayload":
        ga4 = (self.ga4_measurement_id or "").strip().upper() or None
        gtm = (self.gtm_container_id or "").strip().upper() or None
        prop = (self.ga4_property_id or "").strip().replace("properties/", "")
        prop = "".join(ch for ch in prop if ch.isdigit()) or None
        meta_ids = list(dict.fromkeys(re.findall(r"\d+", self.meta_pixel_id or "")))
        meta = ",".join(pixel_id for pixel_id in meta_ids if len(pixel_id) >= 5) or None
        if ga4 and not ga4.startswith("G-"):
            ga4 = None
        if gtm and not gtm.startswith("GTM-"):
            gtm = None
        self.ga4_measurement_id = ga4
        self.gtm_container_id = gtm
        self.ga4_property_id = prop
        self.meta_pixel_id = meta
        return self


class CreateCampaignRequest(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    slug: str | None = None
    default_currency: str | None = None


class UpdateCampaignRequest(BaseModel):
    name: str | None = None
    slug: str | None = None
    status: str | None = None
    default_currency: str | None = None
    stripe_account_id: str | None = None
    paypal_account_id: str | None = None
    nowpayments_account_id: str | None = None
    payment_account_sources: dict[str, str] | None = None
    # None omitted; explicit null clears the minimum (no limit).
    min_donation_amount: float | None = None
    min_donation_amount_once: float | None = None
    min_donation_amount_monthly: float | None = None
    content: CampaignContentPayload | None = None


class CurrencyConfig(BaseModel):
    currency_code: str
    enabled: bool = True
    is_default: bool = False
    amounts_once: list[dict[str, Any]] | None = None
    amounts_monthly: list[dict[str, Any]] | None = None


class DomainRequest(BaseModel):
    hostname: str = Field(min_length=1, max_length=253)


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or secrets.token_hex(4)


def ensure_root_campaign_subdomain() -> None:
    """Link the homepage campaign to {ROOT_CAMPAIGN_SUBDOMAIN}.{PLATFORM_ROOT_DOMAIN}."""
    from site_constants import ROOT_CAMPAIGN_ID

    subdomain = os.getenv("ROOT_CAMPAIGN_SUBDOMAIN", "sudan-needs-you").strip().lower()
    if not subdomain or not platform_root_domain():
        return

    campaign = rest_get_one(
        "campaigns",
        params={"id": f"eq.{ROOT_CAMPAIGN_ID}", "select": "id,slug"},
    )
    if campaign and str(campaign.get("slug") or "") != subdomain:
        rest_patch("campaigns", {"slug": subdomain}, match={"id": ROOT_CAMPAIGN_ID})

    _ensure_campaign_slug_subdomain(ROOT_CAMPAIGN_ID, subdomain)


def _ensure_campaign_slug_subdomain(campaign_id: str, slug: str) -> None:
    """Link {slug}.{PLATFORM_ROOT_DOMAIN} to the campaign when platform DNS is configured."""
    root = platform_root_domain()
    if not root or not slug:
        return

    try:
        hostname, is_platform_subdomain = resolve_campaign_hostname(slug)
    except ValueError:
        return
    if not is_platform_subdomain:
        return

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    verified = {"verified_at": now, "ssl_status": "active"}

    existing_hostname = rest_get_one("domains", params={"hostname": f"eq.{hostname}", "select": "*"})
    if existing_hostname and str(existing_hostname.get("campaign_id")) != campaign_id:
        return

    campaign_domains = rest_get("domains", params={"campaign_id": f"eq.{campaign_id}", "select": "*"})
    for domain in campaign_domains:
        host = str(domain.get("hostname") or "")
        if not host.endswith(f".{root}"):
            continue
        if host == hostname:
            if not domain.get("verified_at"):
                rest_patch("domains", verified, match={"id": domain["id"]})
            return
        rest_patch("domains", {"hostname": hostname, **verified}, match={"id": domain["id"]})
        return

    if not existing_hostname:
        rest_insert(
            "domains",
            {"campaign_id": campaign_id, "hostname": hostname, **verified},
        )


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

    org = rest_get_one(
        "organizations",
        params={"id": f"eq.{org_id}", "select": "default_currency,payment_account_sources"},
    )
    currency = (payload.default_currency or (org or {}).get("default_currency") or "USD")
    currency = str(currency).strip().upper() or "USD"
    payment_sources = normalize_payment_account_sources((org or {}).get("payment_account_sources"))

    campaign = rest_insert(
        "campaigns",
        {
            "organization_id": org_id,
            "name": payload.name,
            "slug": slug,
            "default_currency": currency,
            "status": "draft",
            "payment_account_sources": payment_sources,
        },
    )
    if not campaign:
        err = rest_insert_error(
            "campaigns",
            {
                "organization_id": org_id,
                "name": payload.name,
                "slug": slug,
                "default_currency": currency,
                "status": "draft",
                "payment_account_sources": payment_sources,
            },
        )
        raise HTTPException(status_code=500, detail=err or "Failed to create campaign")
    rest_insert(
        "campaign_content",
        {"campaign_id": campaign["id"], **_default_campaign_content(payload.name)},
    )
    _seed_default_campaign_currencies(str(campaign["id"]), currency)
    _ensure_campaign_slug_subdomain(str(campaign["id"]), slug)
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
    paypal_accounts = rest_get(
        "paypal_accounts",
        params={"organization_id": f"eq.{org_id}", "select": "*"},
    )
    nowpayments_accounts_raw = rest_get(
        "nowpayments_accounts",
        params={"organization_id": f"eq.{org_id}", "select": "*"},
    )
    nowpayments_accounts = [
        {
            "id": row.get("id"),
            "organization_id": row.get("organization_id"),
            "campaign_id": row.get("campaign_id"),
            "api_key_hint": row.get("api_key_hint"),
            "email": row.get("email"),
            "is_default": bool(row.get("is_default")),
            "connection_status": row.get("connection_status") or "active",
        }
        for row in nowpayments_accounts_raw
    ]
    return {
        "campaign": campaign,
        "content": content,
        "currencies": currencies,
        "domains": domains,
        "stripe_accounts": stripe_accounts,
        "paypal_accounts": paypal_accounts,
        "nowpayments_accounts": nowpayments_accounts,
    }


@router.patch("/{org_id}/campaigns/{campaign_id}")
def update_campaign(
    org_id: str,
    campaign_id: str,
    payload: UpdateCampaignRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    payment_fields = (
        "stripe_account_id",
        "paypal_account_id",
        "nowpayments_account_id",
        "payment_account_sources",
    )
    if any(field in payload.model_fields_set for field in payment_fields):
        deny_platform_admin_payment_writes(user)
    existing_campaign = rest_get_one(
        "campaigns",
        params={
            "id": f"eq.{campaign_id}",
            "organization_id": f"eq.{org_id}",
            "select": "id,slug,status",
        },
    )
    if not existing_campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    updates: dict[str, Any] = {}
    if payload.name is not None:
        updates["name"] = payload.name
    if payload.slug is not None:
        slug = payload.slug.strip().lower()
        if not slug:
            raise HTTPException(status_code=400, detail="Campaign slug cannot be empty")
        slug_conflict = rest_get_one(
            "campaigns",
            params={
                "organization_id": f"eq.{org_id}",
                "slug": f"eq.{slug}",
                "select": "id",
            },
        )
        if slug_conflict and str(slug_conflict.get("id")) != campaign_id:
            raise HTTPException(status_code=400, detail=f"Campaign slug '{slug}' already exists in this organization")
        updates["slug"] = slug
    if payload.status is not None:
        updates["status"] = payload.status
    if payload.default_currency is not None:
        updates["default_currency"] = payload.default_currency.upper()
    if payload.stripe_account_id is not None:
        updates["stripe_account_id"] = payload.stripe_account_id or None
    if payload.paypal_account_id is not None:
        updates["paypal_account_id"] = payload.paypal_account_id or None
    if payload.nowpayments_account_id is not None:
        updates["nowpayments_account_id"] = payload.nowpayments_account_id or None
    if payload.payment_account_sources is not None:
        updates["payment_account_sources"] = normalize_payment_account_sources(
            payload.payment_account_sources
        )
    def _normalize_min(raw: float | None) -> float | None:
        if raw is None or not isinstance(raw, (int, float)) or float(raw) <= 0:
            return None
        return round(float(raw), 2)

    once_set = "min_donation_amount_once" in payload.model_fields_set
    monthly_set = "min_donation_amount_monthly" in payload.model_fields_set
    legacy_set = "min_donation_amount" in payload.model_fields_set

    if once_set or monthly_set:
        if once_set:
            updates["min_donation_amount_once"] = _normalize_min(payload.min_donation_amount_once)
        if monthly_set:
            updates["min_donation_amount_monthly"] = _normalize_min(payload.min_donation_amount_monthly)
        # Clear legacy single minimum when frequency-specific values are saved.
        updates["min_donation_amount"] = None
    elif legacy_set:
        legacy = _normalize_min(payload.min_donation_amount)
        updates["min_donation_amount"] = legacy
        updates["min_donation_amount_once"] = legacy
        updates["min_donation_amount_monthly"] = legacy

    if updates:
        rest_patch("campaigns", updates, match={"id": campaign_id})

    resolved_slug = str(updates.get("slug") or existing_campaign.get("slug") or "")
    resolved_status = str(updates.get("status") or existing_campaign.get("status") or "")
    if resolved_slug and resolved_status == "live":
        _ensure_campaign_slug_subdomain(campaign_id, resolved_slug)

    if payload.content:
        content_data = payload.content.normalize_analytics_ids().model_dump()
        # Omit unset pop-up branding so homepage-only saves do not wipe it.
        if content_data.get("popup_view_json") is None:
            content_data.pop("popup_view_json", None)
        if content_data.get("hero_url") and (content_data.get("hero_width") != 1248 or content_data.get("hero_height") != 702):
            raise HTTPException(status_code=400, detail="Hero image must be 1248×702 pixels")
        existing = rest_get_one("campaign_content", params={"campaign_id": f"eq.{campaign_id}", "select": "campaign_id"})
        feed_keys = (
            "show_donor_country",
            "recent_donations_sort",
            "ga4_measurement_id",
            "gtm_container_id",
            "ga4_property_id",
            "meta_pixel_id",
            "title_html",
            "title_font_size",
            "body_font_size",
            "title_html_mobile",
            "body_html_mobile",
            "caption_mobile",
            "title_font_size_mobile",
            "body_font_size_mobile",
        )
        mobile_landing_keys = (
            "title_html_mobile",
            "body_html_mobile",
            "caption_mobile",
            "title_font_size_mobile",
            "body_font_size_mobile",
        )
        if existing:
            updated = rest_patch("campaign_content", content_data, match={"campaign_id": campaign_id})
            # Property ID column may be missing until migration 020 is applied
            if not updated and "ga4_property_id" in content_data:
                without_prop = {k: v for k, v in content_data.items() if k != "ga4_property_id"}
                updated = rest_patch("campaign_content", without_prop, match={"campaign_id": campaign_id})
                if updated:
                    raise HTTPException(
                        status_code=503,
                        detail="Content saved but GA4 Property ID failed: run backend/sql/020_campaign_ga4_property_id.sql on Supabase.",
                    )
            # Meta Pixel column may be missing until migration 021 is applied
            if not updated and "meta_pixel_id" in content_data:
                without_meta = {k: v for k, v in content_data.items() if k != "meta_pixel_id"}
                updated = rest_patch("campaign_content", without_meta, match={"campaign_id": campaign_id})
                if updated:
                    raise HTTPException(
                        status_code=503,
                        detail="Content saved but Meta Pixel ID failed: run backend/sql/021_campaign_meta_pixel_id.sql on Supabase.",
                    )
            if not updated and any(k in content_data for k in mobile_landing_keys):
                without_mobile = {k: v for k, v in content_data.items() if k not in mobile_landing_keys}
                updated = rest_patch("campaign_content", without_mobile, match={"campaign_id": campaign_id})
                if updated:
                    raise HTTPException(
                        status_code=503,
                        detail="Content saved but mobile landing text failed: run backend/sql/025_campaign_mobile_landing_text.sql on Supabase.",
                    )
            # Rich title/text size columns may be missing until migration 023 is applied
            if not updated and any(k in content_data for k in ("title_html", "title_font_size", "body_font_size")):
                without_sizes = {
                    k: v
                    for k, v in content_data.items()
                    if k not in {"title_html", "title_font_size", "body_font_size"}
                }
                updated = rest_patch("campaign_content", without_sizes, match={"campaign_id": campaign_id})
                if updated:
                    raise HTTPException(
                        status_code=503,
                        detail="Content saved but title formatting/text sizes failed: run backend/sql/023_campaign_text_font_sizes.sql and 024_campaign_title_html.sql on Supabase.",
                    )
            if not updated and any(k in content_data for k in feed_keys):
                fallback = {k: v for k, v in content_data.items() if k not in feed_keys}
                updated = rest_patch("campaign_content", fallback, match={"campaign_id": campaign_id})
                if updated:
                    raise HTTPException(
                        status_code=503,
                        detail="Content saved but recent-donations options failed: run backend/sql/010_recent_donations_settings.sql on Supabase.",
                    )
            if not updated and "popup_view_json" in content_data:
                fallback = {k: v for k, v in content_data.items() if k != "popup_view_json"}
                updated = rest_patch("campaign_content", fallback, match={"campaign_id": campaign_id})
                if updated:
                    raise HTTPException(
                        status_code=503,
                        detail="Content saved but pop-up view failed: run backend/sql/005_popup_view_json.sql on Supabase.",
                    )
            if not updated:
                raise HTTPException(status_code=500, detail="Failed to update campaign content")
        else:
            inserted = rest_insert("campaign_content", {"campaign_id": campaign_id, **content_data})
            if not inserted and "ga4_property_id" in content_data:
                without_prop = {k: v for k, v in content_data.items() if k != "ga4_property_id"}
                inserted = rest_insert("campaign_content", {"campaign_id": campaign_id, **without_prop})
                if inserted:
                    raise HTTPException(
                        status_code=503,
                        detail="Content saved but GA4 Property ID failed: run backend/sql/020_campaign_ga4_property_id.sql on Supabase.",
                    )
            if not inserted and "meta_pixel_id" in content_data:
                without_meta = {k: v for k, v in content_data.items() if k != "meta_pixel_id"}
                inserted = rest_insert("campaign_content", {"campaign_id": campaign_id, **without_meta})
                if inserted:
                    raise HTTPException(
                        status_code=503,
                        detail="Content saved but Meta Pixel ID failed: run backend/sql/021_campaign_meta_pixel_id.sql on Supabase.",
                    )
            if not inserted and any(k in content_data for k in mobile_landing_keys):
                without_mobile = {k: v for k, v in content_data.items() if k not in mobile_landing_keys}
                inserted = rest_insert("campaign_content", {"campaign_id": campaign_id, **without_mobile})
                if inserted:
                    raise HTTPException(
                        status_code=503,
                        detail="Content saved but mobile landing text failed: run backend/sql/025_campaign_mobile_landing_text.sql on Supabase.",
                    )
            if not inserted and any(k in content_data for k in ("title_html", "title_font_size", "body_font_size")):
                without_sizes = {
                    k: v
                    for k, v in content_data.items()
                    if k not in {"title_html", "title_font_size", "body_font_size"}
                }
                inserted = rest_insert("campaign_content", {"campaign_id": campaign_id, **without_sizes})
                if inserted:
                    raise HTTPException(
                        status_code=503,
                        detail="Content saved but title formatting/text sizes failed: run backend/sql/023_campaign_text_font_sizes.sql and 024_campaign_title_html.sql on Supabase.",
                    )
            if not inserted and any(k in content_data for k in feed_keys):
                fallback = {k: v for k, v in content_data.items() if k not in feed_keys}
                inserted = rest_insert("campaign_content", {"campaign_id": campaign_id, **fallback})
                if inserted:
                    raise HTTPException(
                        status_code=503,
                        detail="Content saved but recent-donations options failed: run backend/sql/010_recent_donations_settings.sql on Supabase.",
                    )
            if not inserted and "popup_view_json" in content_data:
                fallback = {k: v for k, v in content_data.items() if k != "popup_view_json"}
                inserted = rest_insert("campaign_content", {"campaign_id": campaign_id, **fallback})
                if inserted:
                    raise HTTPException(
                        status_code=503,
                        detail="Content saved but pop-up view failed: run backend/sql/005_popup_view_json.sql on Supabase.",
                    )
            if not inserted:
                raise HTTPException(status_code=500, detail="Failed to create campaign content")

    return get_campaign(org_id, campaign_id, user)


@router.delete("/{org_id}/campaigns/{campaign_id}")
def delete_campaign(
    org_id: str,
    campaign_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    if campaign_id == ROOT_CAMPAIGN_ID:
        raise HTTPException(status_code=400, detail="The platform root campaign cannot be deleted")

    campaign = rest_get_one(
        "campaigns",
        params={
            "id": f"eq.{campaign_id}",
            "organization_id": f"eq.{org_id}",
            "select": "id,name",
        },
    )
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Donations reference campaigns without ON DELETE CASCADE — keep gift history.
    rest_patch("donations", {"campaign_id": None}, match={"campaign_id": campaign_id})

    if not rest_delete("campaigns", match={"id": campaign_id, "organization_id": org_id}):
        raise HTTPException(
            status_code=500,
            detail="Failed to delete campaign. It may still be linked to other records.",
        )
    return {"deleted": True, "id": campaign_id, "name": campaign.get("name")}


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


@router.get("/{org_id}/domains")
def list_org_domains(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    _get_org_id(user, org_id)
    campaigns = rest_get(
        "campaigns",
        params={"organization_id": f"eq.{org_id}", "select": "id,name,status,slug"},
    )
    campaign_ids = [str(c["id"]) for c in campaigns]
    if not campaign_ids:
        return {"domains": [], **platform_domain_config(), "cname_target": _cname_target()}

    domains = rest_get(
        "domains",
        params={
            "campaign_id": f"in.({','.join(campaign_ids)})",
            "select": "*",
            "order": "created_at.desc",
        },
    )
    campaigns_by_id = {str(c["id"]): c for c in campaigns}
    return {
        "domains": [_enrich_domain_row(domain, campaigns_by_id) for domain in domains],
        "campaigns": campaigns,
        **platform_domain_config(),
        "cname_target": _cname_target(),
    }


@router.get("/{org_id}/campaigns/{campaign_id}/domains/config")
def get_domain_config(
    org_id: str,
    campaign_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, str | bool | None]:
    _get_org_id(user, org_id)
    _ = campaign_id
    config = platform_domain_config()
    return {
        **config,
        "cname_target": _cname_target(),
    }


@router.post("/{org_id}/campaigns/{campaign_id}/domains")
def add_domain(
    org_id: str,
    campaign_id: str,
    payload: DomainRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    campaign = rest_get_one(
        "campaigns",
        params={"id": f"eq.{campaign_id}", "organization_id": f"eq.{org_id}", "select": "id,name,status"},
    )
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    try:
        hostname, is_platform_subdomain = resolve_campaign_hostname(payload.hostname)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    root = platform_root_domain()
    label = subdomain_label_from_hostname(hostname) or payload.hostname.strip().lower()

    existing_hostname = rest_get_one("domains", params={"hostname": f"eq.{hostname}", "select": "*"})
    if existing_hostname:
        owner = rest_get_one(
            "campaigns",
            params={"id": f"eq.{existing_hostname['campaign_id']}", "select": "name,organization_id"},
        )
        owner_name = owner.get("name") if owner else "another campaign"
        raise HTTPException(
            status_code=409,
            detail=f'Subdomain "{label}" is already taken by campaign "{owner_name}". Choose a different name.',
        )

    if is_platform_subdomain and root:
        existing_for_campaign = rest_get(
            "domains",
            params={"campaign_id": f"eq.{campaign_id}", "select": "*"},
        )
        for existing in existing_for_campaign:
            existing_host = str(existing.get("hostname") or "")
            if existing_host.endswith(f".{root}"):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f'Campaign "{campaign["name"]}" already uses subdomain '
                        f'"{subdomain_label_from_hostname(existing_host) or existing_host}". '
                        "Remove it first to assign a new one."
                    ),
                )

    row: dict[str, Any] = {"campaign_id": campaign_id, "hostname": hostname}
    if is_platform_subdomain:
        from datetime import datetime, timezone

        row["verified_at"] = datetime.now(timezone.utc).isoformat()
        row["ssl_status"] = "active"

    domain = rest_insert("domains", row)
    if not domain:
        err = rest_insert_error("domains", row)
        raise HTTPException(status_code=400, detail=err or "Could not add subdomain")
    token = str(domain.get("verification_token") or "")
    enriched = _enrich_domain_row(domain, {campaign_id: campaign})
    return {
        **enriched,
        "resolved_hostname": hostname,
        "auto_configured": is_platform_subdomain,
        "dns_instructions": build_dns_instructions(hostname, token, auto_configured=is_platform_subdomain),
    }


@router.delete("/{org_id}/campaigns/{campaign_id}/domains/{domain_id}")
def delete_domain(
    org_id: str,
    campaign_id: str,
    domain_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, str]:
    require_org_access(org_id, user, min_role="admin")
    campaign = rest_get_one(
        "campaigns",
        params={"id": f"eq.{campaign_id}", "organization_id": f"eq.{org_id}", "select": "id"},
    )
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    domain = rest_get_one(
        "domains",
        params={"id": f"eq.{domain_id}", "campaign_id": f"eq.{campaign_id}", "select": "id"},
    )
    if not domain:
        raise HTTPException(status_code=404, detail="Subdomain not found")
    if not rest_delete("domains", match={"id": domain_id, "campaign_id": campaign_id}):
        raise HTTPException(status_code=500, detail="Failed to remove subdomain")
    return {"status": "deleted"}


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
    rows = rest_get(
        "organization_members",
        params={
            "organization_id": f"eq.{org_id}",
            "select": "id,user_id,role,created_at,profiles(id,first_name,last_name,role)",
        },
    )
    from invite_service import get_user_email_by_id

    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        email = get_user_email_by_id(str(row.get("user_id") or ""))
        item["email"] = email
        out.append(item)
    return out


class TeamInviteRequest(BaseModel):
    email: str = Field(min_length=3)
    role: str = "member"


@router.post("/{org_id}/invites")
def invite_member(
    org_id: str,
    payload: TeamInviteRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    org = rest_get_one("organizations", params={"id": f"eq.{org_id}", "select": "id,name"})
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    role = str(payload.role or "member").strip().lower()
    if role not in {"owner", "admin", "member"}:
        raise HTTPException(status_code=400, detail="Invalid role")
    invite = rest_insert(
        "organization_invites",
        {
            "organization_id": org_id,
            "email": payload.email.lower(),
            "role": role,
            "invited_by": user.id,
        },
    )
    if not invite:
        raise HTTPException(status_code=400, detail="Failed to create invite")
    if not invite.get("token"):
        invite = rest_get_one(
            "organization_invites",
            params={"id": f"eq.{invite['id']}", "select": "*"},
        ) or invite
    provisioned = send_pending_organization_invite(
        invite,
        organization_name=str(org.get("name") or "your organization"),
    )
    return {
        "invite": invite,
        "email_sent": bool(provisioned.get("email_sent")),
        "invite_url": provisioned.get("invite_url"),
        "message": (
            f"Invitation emailed to {payload.email.lower()}."
            if provisioned.get("email_sent")
            else f"Invite created. Configure RESEND_API_KEY to email {payload.email.lower()}."
        ),
    }


@router.get("/{org_id}/settings")
def get_org_settings(org_id: str, user: Annotated[AuthUser, Depends(require_auth)]) -> dict[str, Any]:
    _get_org_id(user, org_id)
    org = rest_get_one(
        "organizations",
        params={
            "id": f"eq.{org_id}",
            "select": "id,name,slug,default_currency,reporting_currency,timezone,payment_methods,notification_prefs,reminder_interval_days,email_organization_name,payment_account_sources",
        },
    )
    if not org:
        org = rest_get_one(
            "organizations",
            params={
                "id": f"eq.{org_id}",
                "select": "id,name,slug,default_currency,reporting_currency,timezone,payment_methods,notification_prefs,reminder_interval_days,email_organization_name",
            },
        )
    if not org:
        org = rest_get_one(
            "organizations",
            params={
                "id": f"eq.{org_id}",
                "select": "id,name,slug,default_currency,reporting_currency,timezone,payment_methods,notification_prefs,reminder_interval_days",
            },
        )
    if not org:
        org = rest_get_one(
            "organizations",
            params={
                "id": f"eq.{org_id}",
                "select": "id,name,slug,default_currency,reporting_currency,timezone,payment_methods,notification_prefs",
            },
        )
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if org.get("reminder_interval_days") is None:
        org["reminder_interval_days"] = 7
    if "email_organization_name" not in org:
        org["email_organization_name"] = None
    org["payment_account_sources"] = normalize_payment_account_sources(org.get("payment_account_sources"))
    return org


@router.get("/{org_id}/platform-payment-accounts")
def get_platform_payment_accounts(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="member")
    from routers.payment_accounts import homepage_payment_summary

    return homepage_payment_summary()


def _cascade_org_default_currency(org_id: str, currency: str) -> int:
    """Push org default currency onto every campaign in the organization."""
    code = currency.strip().upper()
    if not code:
        return 0
    campaigns = rest_get(
        "campaigns",
        params={"organization_id": f"eq.{org_id}", "select": "id"},
    )
    updated = 0
    for campaign in campaigns:
        cid = str(campaign.get("id") or "")
        if not cid:
            continue
        rest_patch("campaigns", {"default_currency": code}, match={"id": cid})
        updated += 1
        default_rows = rest_get(
            "campaign_currencies",
            params={
                "campaign_id": f"eq.{cid}",
                "is_default": "eq.true",
                "select": "id,currency_code",
            },
        )
        for row in default_rows:
            if str(row.get("currency_code") or "").upper() == code:
                continue
            conflict = rest_get_one(
                "campaign_currencies",
                params={
                    "campaign_id": f"eq.{cid}",
                    "currency_code": f"eq.{code}",
                    "select": "id",
                },
            )
            if conflict:
                rest_patch("campaign_currencies", {"is_default": True}, match={"id": conflict["id"]})
                rest_patch("campaign_currencies", {"is_default": False}, match={"id": row["id"]})
            else:
                rest_patch("campaign_currencies", {"currency_code": code}, match={"id": row["id"]})
    return updated


@router.patch("/{org_id}/settings")
def update_org_settings(
    org_id: str,
    payload: dict[str, Any],
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    if "payment_methods" in payload or "payment_account_sources" in payload:
        deny_platform_admin_payment_writes(user)
    existing = rest_get_one(
        "organizations",
        params={
            "id": f"eq.{org_id}",
            "select": "id,name,slug,default_currency,reporting_currency,timezone,reminder_interval_days",
        },
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Organization not found")

    allowed = {
        "name",
        "slug",
        "default_currency",
        "reporting_currency",
        "timezone",
        "payment_methods",
        "notification_prefs",
        "reminder_interval_days",
        "email_organization_name",
        "payment_account_sources",
    }
    updates: dict[str, Any] = {k: v for k, v in payload.items() if k in allowed}

    if "name" in updates:
        name = str(updates["name"] or "").strip()
        if len(name) < 2:
            raise HTTPException(status_code=400, detail="Organization name must be at least 2 characters")
        updates["name"] = name

    if "slug" in updates:
        slug = _slugify(str(updates["slug"] or ""))
        if len(slug) < 2:
            raise HTTPException(status_code=400, detail="Organization slug must be at least 2 characters")
        conflict = rest_get_one(
            "organizations",
            params={"slug": f"eq.{slug}", "select": "id"},
        )
        if conflict and str(conflict.get("id")) != org_id:
            raise HTTPException(status_code=400, detail=f"Organization slug '{slug}' is already taken")
        updates["slug"] = slug

    currency_changed = False
    if "default_currency" in updates and updates["default_currency"] is not None:
        updates["default_currency"] = str(updates["default_currency"]).strip().upper()
        if not updates["default_currency"]:
            raise HTTPException(status_code=400, detail="Default currency is required")
        currency_changed = updates["default_currency"] != str(existing.get("default_currency") or "").upper()

    if "reporting_currency" in updates and updates["reporting_currency"] is not None:
        updates["reporting_currency"] = str(updates["reporting_currency"]).strip().upper()
        if not updates["reporting_currency"]:
            raise HTTPException(status_code=400, detail="Reporting currency is required")

    if "timezone" in updates:
        timezone = str(updates["timezone"] or "").strip()
        if not timezone:
            raise HTTPException(status_code=400, detail="Timezone is required")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid IANA timezone") from exc
        updates["timezone"] = timezone

    if "reminder_interval_days" in updates:
        try:
            days = int(updates["reminder_interval_days"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Reminder interval must be a whole number of days")
        if days < 1 or days > 365:
            raise HTTPException(status_code=400, detail="Reminder interval must be between 1 and 365 days")
        updates["reminder_interval_days"] = days

    if "email_organization_name" in updates:
        raw = updates["email_organization_name"]
        if raw is None:
            updates["email_organization_name"] = None
        else:
            name = str(raw).strip()
            updates["email_organization_name"] = name[:160] if name else None

    if "payment_account_sources" in updates:
        updates["payment_account_sources"] = normalize_payment_account_sources(
            updates["payment_account_sources"]
        )

    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")

    updated = rest_patch("organizations", updates, match={"id": org_id})
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update organization")

    campaigns_updated = 0
    if currency_changed:
        campaigns_updated = _cascade_org_default_currency(org_id, str(updates["default_currency"]))

    # Return fresh settings including slug for the admin UI.
    fresh = rest_get_one(
        "organizations",
        params={
            "id": f"eq.{org_id}",
            "select": "id,name,slug,default_currency,reporting_currency,timezone,payment_methods,notification_prefs,reminder_interval_days,email_organization_name,payment_account_sources",
        },
    ) or updated
    if isinstance(fresh, dict) and "email_organization_name" not in fresh:
        fresh = {**fresh, "email_organization_name": None}
    if isinstance(fresh, dict):
        fresh = {
            **fresh,
            "payment_account_sources": normalize_payment_account_sources(
                fresh.get("payment_account_sources")
            ),
            "campaigns_currency_updated": campaigns_updated,
        }
    return fresh
