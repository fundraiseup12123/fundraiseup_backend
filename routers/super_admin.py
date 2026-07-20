from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field

from auth import AuthUser, require_auth, require_super_admin
from db import rest_delete, rest_get, rest_get_one, rest_insert, rest_patch, select_columns
from routers.organizations import (
    CampaignContentPayload,
    PopupViewPayload,
    _cascade_org_default_currency,
    _slugify as org_slugify,
)
from invite_service import fulfill_organization_invite, fulfill_platform_admin_invite

router = APIRouter(prefix="/super", tags=["super-admin"])


def _parse_popup_view(content: dict[str, Any] | None) -> dict[str, Any] | None:
    if not content:
        return None
    raw = content.get("popup_view_json")
    if not raw:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


class RootBrandingPayload(CampaignContentPayload):
    popup_view: PopupViewPayload | None = None

ROOT_CAMPAIGN_ID = os.getenv("ROOT_CAMPAIGN_ID", "00000000-0000-4000-8000-000000000002")


class CreateOrganizationRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    slug: str | None = None
    default_currency: str = "USD"
    admin_email: EmailStr
    admin_first_name: str = ""
    admin_last_name: str = ""


class OrganizationResponse(BaseModel):
    id: str
    name: str
    slug: str
    status: str
    default_currency: str
    created_at: str | None = None


class InviteResponse(BaseModel):
    id: str
    email: str
    token: str
    organization_id: str
    expires_at: str


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or secrets.token_hex(4)


@router.get("/organizations", response_model=list[OrganizationResponse])
def list_organizations(user: Annotated[AuthUser, Depends(require_super_admin)]) -> list[OrganizationResponse]:
    rows = rest_get(
        "organizations",
        params={
            "select": select_columns("id", "name", "slug", "status", "default_currency", "created_at"),
            "order": "created_at.desc",
        },
    )
    return [
        OrganizationResponse(
            id=str(r["id"]),
            name=r["name"],
            slug=r["slug"],
            status=r["status"],
            default_currency=r["default_currency"],
            created_at=r.get("created_at"),
        )
        for r in rows
    ]


@router.post("/organizations", response_model=dict[str, Any])
def create_organization(
    payload: CreateOrganizationRequest,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    slug = payload.slug or _slugify(payload.name)
    existing = rest_get_one("organizations", params={"slug": f"eq.{slug}", "select": "id"})
    if existing:
        raise HTTPException(status_code=400, detail="Organization slug already exists")

    org = rest_insert(
        "organizations",
        {
            "name": payload.name,
            "slug": slug,
            "default_currency": payload.default_currency.upper(),
            "reporting_currency": payload.default_currency.upper(),
            "created_by": user.id,
        },
    )
    if not org:
        raise HTTPException(status_code=500, detail="Failed to create organization")

    org_id = org["id"]
    invite = rest_insert(
        "organization_invites",
        {
            "organization_id": org_id,
            "email": payload.admin_email.lower(),
            "role": "owner",
            "invited_by": user.id,
        },
    )

    default_campaign = rest_insert(
        "campaigns",
        {
            "organization_id": org_id,
            "slug": "main",
            "name": f"{payload.name} Campaign",
            "status": "draft",
            "default_currency": payload.default_currency.upper(),
        },
    )
    if default_campaign:
        from site_constants import DEFAULT_CAMPAIGN_CONTENT

        rest_insert(
            "campaign_content",
            {"campaign_id": default_campaign["id"], **DEFAULT_CAMPAIGN_CONTENT, "title": payload.name},
        )

    provisioned = None
    provision_error: str | None = None
    if invite:
        try:
            provisioned = fulfill_organization_invite(
                invite,
                organization_name=payload.name,
                first_name=payload.admin_first_name,
                last_name=payload.admin_last_name,
            )
        except HTTPException as exc:
            provision_error = str(exc.detail) if isinstance(exc.detail, str) else "Admin invite setup failed"
        except Exception as exc:
            provision_error = str(exc) or "Admin invite setup failed"

    if provision_error:
        message = (
            f"Organization created, but admin setup could not finish: {provision_error}. "
            f"You can resend an invite from the organization page."
        )
    elif provisioned and provisioned.get("email_sent"):
        message = f"Organization created. Login details emailed to {payload.admin_email.lower()}."
    else:
        message = (
            f"Organization created. Configure RESEND_API_KEY to email login details to "
            f"{payload.admin_email.lower()}."
        )

    return {
        "organization": org,
        "invite": invite,
        "campaign": default_campaign,
        "email_sent": bool(provisioned and provisioned.get("email_sent")),
        "login_url": provisioned.get("login_url") if provisioned else None,
        "provision_error": provision_error,
        "message": message,
    }


def _root_campaign_bundle() -> dict[str, Any]:
    campaign = rest_get_one("campaigns", params={"id": f"eq.{ROOT_CAMPAIGN_ID}", "select": "*"})
    if not campaign:
        raise HTTPException(status_code=404, detail="Root campaign not found. Run sql/002_seed_sudan_campaign.sql")
    content = rest_get_one("campaign_content", params={"campaign_id": f"eq.{ROOT_CAMPAIGN_ID}", "select": "*"})
    return {
        "campaign": campaign,
        "content": content or {},
        "popup_view": _parse_popup_view(content),
    }


@router.get("/root-branding")
def get_root_branding(user: Annotated[AuthUser, Depends(require_super_admin)]) -> dict[str, Any]:
    return _root_campaign_bundle()


@router.patch("/root-branding")
def update_root_branding(
    payload: RootBrandingPayload,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    content_data = payload.model_dump(
        exclude={"popup_view", "popup_view_json"},
        exclude_none=True,
    )
    # Always persist rich title/text sizes so Reset can clear previous values.
    content_data["title_html"] = payload.title_html
    content_data["title_font_size"] = payload.title_font_size
    content_data["body_font_size"] = payload.body_font_size
    content_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    if payload.popup_view is not None:
        existing = rest_get_one(
            "campaign_content",
            params={"campaign_id": f"eq.{ROOT_CAMPAIGN_ID}", "select": "popup_view_json"},
        )
        merged = {**(_parse_popup_view(existing) or {}), **payload.popup_view.model_dump(exclude_none=True)}
        merged["modal_title_html"] = payload.popup_view.modal_title_html
        merged["modal_title_font_size"] = payload.popup_view.modal_title_font_size
        merged["modal_body_font_size"] = payload.popup_view.modal_body_font_size
        content_data["popup_view_json"] = json.dumps(merged)
    existing = rest_get_one("campaign_content", params={"campaign_id": f"eq.{ROOT_CAMPAIGN_ID}", "select": "campaign_id"})
    if existing:
        updated = rest_patch("campaign_content", content_data, match={"campaign_id": ROOT_CAMPAIGN_ID})
        if not updated and any(k in content_data for k in ("title_html", "title_font_size", "body_font_size")):
            without_sizes = {
                k: v
                for k, v in content_data.items()
                if k not in {"title_html", "title_font_size", "body_font_size"}
            }
            updated = rest_patch("campaign_content", without_sizes, match={"campaign_id": ROOT_CAMPAIGN_ID})
            if updated:
                raise HTTPException(
                    status_code=503,
                    detail="Homepage saved but title formatting/text sizes failed: run backend/sql/023_campaign_text_font_sizes.sql and 024_campaign_title_html.sql on Supabase.",
                )
        if not updated and "popup_view_json" in content_data:
            fallback = {k: v for k, v in content_data.items() if k != "popup_view_json"}
            updated = rest_patch("campaign_content", fallback, match={"campaign_id": ROOT_CAMPAIGN_ID})
            if updated:
                raise HTTPException(
                    status_code=503,
                    detail="Homepage saved but pop-up view failed: run backend/sql/005_popup_view_json.sql on Supabase.",
                )
        if not updated:
            raise HTTPException(status_code=500, detail="Failed to update root branding")
    else:
        inserted = rest_insert("campaign_content", {"campaign_id": ROOT_CAMPAIGN_ID, **content_data})
        if not inserted and any(k in content_data for k in ("title_html", "title_font_size", "body_font_size")):
            without_sizes = {
                k: v
                for k, v in content_data.items()
                if k not in {"title_html", "title_font_size", "body_font_size"}
            }
            inserted = rest_insert("campaign_content", {"campaign_id": ROOT_CAMPAIGN_ID, **without_sizes})
            if inserted:
                raise HTTPException(
                    status_code=503,
                    detail="Homepage saved but title formatting/text sizes failed: run backend/sql/023_campaign_text_font_sizes.sql and 024_campaign_title_html.sql on Supabase.",
                )
        if not inserted:
            raise HTTPException(status_code=500, detail="Failed to create root branding content")
    return _root_campaign_bundle()


@router.get("/root-donations")
def get_root_donations(
    user: Annotated[AuthUser, Depends(require_super_admin)],
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    from site_constants import ROOT_ORG_ID

    select_cols = select_columns(
        "id", "first_name", "last_name", "email", "amount", "currency",
        "frequency", "honoree_name", "payment_method", "created_at", "status",
        "campaign_id", "organization_id",
    )
    rows = rest_get(
        "donations",
        params={
            "campaign_id": f"eq.{ROOT_CAMPAIGN_ID}",
            "select": select_cols,
            "order": "created_at.desc",
            "limit": str(limit + 1),
            "offset": str(offset),
        },
    )

    # Older PayPal homepage gifts may have null campaign_id / organization_id.
    if offset == 0:
        orphan_rows = rest_get(
            "donations",
            params={
                "payment_method": "eq.paypal",
                "or": f"(campaign_id.is.null,and(organization_id.is.null,campaign_id.eq.{ROOT_CAMPAIGN_ID}))",
                "select": select_cols,
                "order": "created_at.desc",
                "limit": str(limit),
            },
        )
        if orphan_rows:
            seen = {str(r.get("id")) for r in rows}
            for row in orphan_rows:
                # Keep only true root orphans (no campaign, or root campaign with null org).
                camp = row.get("campaign_id")
                org = row.get("organization_id")
                if camp and camp != ROOT_CAMPAIGN_ID:
                    continue
                if org and org != ROOT_ORG_ID:
                    continue
                row_id = str(row.get("id") or "")
                if row_id and row_id not in seen:
                    rows.append(row)
                    seen.add(row_id)
            rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)

    has_more = len(rows) > limit
    return {"donations": rows[:limit], "has_more": has_more}


class UpdateOrganizationRequest(BaseModel):
    name: str | None = None
    slug: str | None = None
    status: str | None = None
    default_currency: str | None = None
    reporting_currency: str | None = None


@router.patch("/organizations/{org_id}")
def update_organization(
    org_id: str,
    payload: UpdateOrganizationRequest,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    existing = rest_get_one(
        "organizations",
        params={"id": f"eq.{org_id}", "select": "id,default_currency"},
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Organization not found")

    updates: dict[str, Any] = {}
    if payload.name is not None:
        name = payload.name.strip()
        if len(name) < 2:
            raise HTTPException(status_code=400, detail="Organization name must be at least 2 characters")
        updates["name"] = name
    if payload.slug is not None:
        slug = org_slugify(payload.slug)
        if len(slug) < 2:
            raise HTTPException(status_code=400, detail="Organization slug must be at least 2 characters")
        conflict = rest_get_one("organizations", params={"slug": f"eq.{slug}", "select": "id"})
        if conflict and str(conflict.get("id")) != org_id:
            raise HTTPException(status_code=400, detail=f"Organization slug '{slug}' is already taken")
        updates["slug"] = slug
    if payload.status is not None:
        updates["status"] = payload.status
    currency_changed = False
    if payload.default_currency is not None:
        updates["default_currency"] = payload.default_currency.upper()
        currency_changed = updates["default_currency"] != str(existing.get("default_currency") or "").upper()
    if payload.reporting_currency is not None:
        updates["reporting_currency"] = payload.reporting_currency.upper()
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")
    updated = rest_patch("organizations", updates, match={"id": org_id})
    if not updated:
        raise HTTPException(status_code=404, detail="Organization not found")
    if currency_changed:
        _cascade_org_default_currency(org_id, str(updates["default_currency"]))
    return updated


@router.delete("/organizations/{org_id}")
def delete_organization(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, str]:
    deleted = rest_delete("organizations", match={"id": org_id})
    if not deleted:
        raise HTTPException(status_code=404, detail="Organization not found")
    return {"status": "deleted"}


class OrgAdminInviteRequest(BaseModel):
    email: EmailStr
    role: str = "admin"
    organization_id: str | None = None
    access_scope: str = Field(default="organization", pattern="^(organization|all)$")


class UpdateOrgAdminRequest(BaseModel):
    role: str = Field(pattern="^(owner|admin|member)$")


@router.get("/organization-admins")
def list_organization_admins(
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> list[dict[str, Any]]:
    members = rest_get(
        "organization_members",
        params={
            "select": "id,organization_id,user_id,role,created_at",
            "order": "created_at.desc",
        },
    )
    org_ids = list({m["organization_id"] for m in members})
    orgs_by_id: dict[str, dict[str, Any]] = {}
    if org_ids:
        org_rows = rest_get(
            "organizations",
            params={
                "id": f"in.({','.join(org_ids)})",
                "select": "id,name,slug",
            },
        )
        orgs_by_id = {str(o["id"]): o for o in org_rows}

    user_ids = list({m["user_id"] for m in members})
    profiles_by_id: dict[str, dict[str, Any]] = {}
    if user_ids:
        profile_rows = rest_get(
            "profiles",
            params={
                "id": f"in.({','.join(user_ids)})",
                "select": "id,first_name,last_name,role",
            },
        )
        profiles_by_id = {str(p["id"]): p for p in profile_rows}

    result: list[dict[str, Any]] = []
    for member in members:
        if member.get("role") not in ("admin", "owner"):
            continue
        org = orgs_by_id.get(str(member["organization_id"]), {})
        profile = profiles_by_id.get(str(member["user_id"]), {})
        result.append({
            "id": member["id"],
            "organization_id": member["organization_id"],
            "organization_name": org.get("name", ""),
            "organization_slug": org.get("slug", ""),
            "user_id": member["user_id"],
            "role": member["role"],
            "access_scope": "organization",
            "first_name": profile.get("first_name"),
            "last_name": profile.get("last_name"),
            "created_at": member.get("created_at"),
        })

    platform_rows = rest_get(
        "profiles",
        params={
            "role": "eq.platform_admin",
            "select": "id,first_name,last_name,role",
        },
    ) or []
    for profile in platform_rows:
        result.append({
            "id": f"platform:{profile['id']}",
            "organization_id": None,
            "organization_name": "All organizations",
            "organization_slug": "",
            "user_id": profile["id"],
            "role": "platform_admin",
            "access_scope": "all",
            "first_name": profile.get("first_name"),
            "last_name": profile.get("last_name"),
            "created_at": None,
        })
    return result


@router.post("/organization-admins")
def invite_organization_admin(
    payload: OrgAdminInviteRequest,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    email = str(payload.email).lower()
    if payload.access_scope == "all":
        provisioned = fulfill_platform_admin_invite(
            email=email,
            invited_by=user.id,
        )
        return {
            "email_sent": bool(provisioned.get("email_sent")),
            "login_url": provisioned.get("login_url"),
            "access_scope": "all",
            "message": (
                f"Platform admin login details emailed to {email}."
                if provisioned.get("email_sent")
                else f"Platform admin account created. Configure RESEND_API_KEY to email login details to {email}."
            ),
        }

    if not payload.organization_id:
        raise HTTPException(status_code=400, detail="organization_id is required")
    org = rest_get_one("organizations", params={"id": f"eq.{payload.organization_id}", "select": "id,name"})
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    invite = rest_insert(
        "organization_invites",
        {
            "organization_id": payload.organization_id,
            "email": email,
            "role": payload.role,
            "invited_by": user.id,
        },
    )
    if not invite:
        raise HTTPException(status_code=400, detail="Failed to create invite")
    provisioned = fulfill_organization_invite(
        invite,
        organization_name=str(org.get("name") or "your organization"),
    )
    return {
        "invite": invite,
        "email_sent": bool(provisioned.get("email_sent")),
        "login_url": provisioned.get("login_url"),
        "access_scope": "organization",
        "message": (
            f"Login details emailed to {email}."
            if provisioned.get("email_sent")
            else f"Admin account created. Configure RESEND_API_KEY to email login details to {email}."
        ),
    }


@router.patch("/organization-admins/{member_id}")
def update_organization_admin(
    member_id: str,
    payload: UpdateOrgAdminRequest,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    if member_id.startswith("platform:"):
        raise HTTPException(status_code=400, detail="Platform admin role cannot be changed here")
    updated = rest_patch("organization_members", {"role": payload.role}, match={"id": member_id})
    if not updated:
        raise HTTPException(status_code=404, detail="Member not found")
    return updated


@router.delete("/organization-admins/{member_id}")
def delete_organization_admin(
    member_id: str,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, str]:
    if member_id.startswith("platform:"):
        user_id = member_id.split(":", 1)[1]
        profile = rest_get_one(
            "profiles",
            params={"id": f"eq.{user_id}", "select": "id,role"},
        )
        if not profile or str(profile.get("role") or "") != "platform_admin":
            raise HTTPException(status_code=404, detail="Platform admin not found")
        updated = rest_patch("profiles", {"role": "org_user"}, match={"id": user_id})
        if not updated:
            raise HTTPException(status_code=500, detail="Failed to remove platform admin")
        return {"status": "deleted"}

    deleted = rest_delete("organization_members", match={"id": member_id})
    if not deleted:
        raise HTTPException(status_code=404, detail="Member not found")
    return {"status": "deleted"}


@router.get("/me")
def get_me(user: Annotated[AuthUser, Depends(require_auth)]) -> dict[str, Any]:
    if user.role in ("super_admin", "platform_admin"):
        organizations = rest_get(
            "organizations",
            params={"select": "id,name,slug", "order": "name.asc"},
        )
    elif user.organization_ids:
        organizations = rest_get(
            "organizations",
            params={
                "id": f"in.({','.join(user.organization_ids)})",
                "select": "id,name,slug",
            },
        )
    else:
        organizations = []
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "organization_ids": user.organization_ids,
        "org_roles": user.org_roles,
        "is_super_admin": user.role == "super_admin",
        "is_platform_admin": user.role == "platform_admin",
        "organizations": organizations,
    }
