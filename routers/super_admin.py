from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field

from auth import AuthUser, require_auth, require_super_admin
from db import rest_delete, rest_get, rest_get_one, rest_insert, rest_patch, select_columns
from routers.organizations import CampaignContentPayload

router = APIRouter(prefix="/super", tags=["super-admin"])

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

    return {
        "organization": org,
        "invite": invite,
        "campaign": default_campaign,
        "invite_url": f"/invite/{invite['token']}" if invite else None,
    }


def _root_campaign_bundle() -> dict[str, Any]:
    campaign = rest_get_one("campaigns", params={"id": f"eq.{ROOT_CAMPAIGN_ID}", "select": "*"})
    if not campaign:
        raise HTTPException(status_code=404, detail="Root campaign not found. Run sql/002_seed_sudan_campaign.sql")
    content = rest_get_one("campaign_content", params={"campaign_id": f"eq.{ROOT_CAMPAIGN_ID}", "select": "*"})
    return {"campaign": campaign, "content": content or {}}


@router.get("/root-branding")
def get_root_branding(user: Annotated[AuthUser, Depends(require_super_admin)]) -> dict[str, Any]:
    return _root_campaign_bundle()


@router.patch("/root-branding")
def update_root_branding(
    payload: CampaignContentPayload,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    content_data = payload.model_dump()
    content_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    existing = rest_get_one("campaign_content", params={"campaign_id": f"eq.{ROOT_CAMPAIGN_ID}", "select": "campaign_id"})
    if existing:
        updated = rest_patch("campaign_content", content_data, match={"campaign_id": ROOT_CAMPAIGN_ID})
        if not updated:
            raise HTTPException(status_code=500, detail="Failed to update root branding")
    else:
        inserted = rest_insert("campaign_content", {"campaign_id": ROOT_CAMPAIGN_ID, **content_data})
        if not inserted:
            raise HTTPException(status_code=500, detail="Failed to create root branding content")
    return _root_campaign_bundle()


@router.get("/root-donations")
def get_root_donations(
    user: Annotated[AuthUser, Depends(require_super_admin)],
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    rows = rest_get(
        "donations",
        params={
            "campaign_id": f"eq.{ROOT_CAMPAIGN_ID}",
            "select": select_columns(
                "id", "first_name", "last_name", "email", "amount", "currency",
                "frequency", "honoree_name", "payment_method", "created_at", "status",
            ),
            "order": "created_at.desc",
            "limit": str(limit + 1),
            "offset": str(offset),
        },
    )
    has_more = len(rows) > limit
    return {"donations": rows[:limit], "has_more": has_more}


class UpdateOrganizationRequest(BaseModel):
    name: str | None = None
    status: str | None = None
    default_currency: str | None = None


@router.patch("/organizations/{org_id}")
def update_organization(
    org_id: str,
    payload: UpdateOrganizationRequest,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if payload.name is not None:
        updates["name"] = payload.name
    if payload.status is not None:
        updates["status"] = payload.status
    if payload.default_currency is not None:
        updates["default_currency"] = payload.default_currency.upper()
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")
    updated = rest_patch("organizations", updates, match={"id": org_id})
    if not updated:
        raise HTTPException(status_code=404, detail="Organization not found")
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
    organization_id: str
    email: EmailStr
    role: str = "admin"


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
            "first_name": profile.get("first_name"),
            "last_name": profile.get("last_name"),
            "created_at": member.get("created_at"),
        })
    return result


@router.post("/organization-admins")
def invite_organization_admin(
    payload: OrgAdminInviteRequest,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    org = rest_get_one("organizations", params={"id": f"eq.{payload.organization_id}", "select": "id"})
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    invite = rest_insert(
        "organization_invites",
        {
            "organization_id": payload.organization_id,
            "email": payload.email.lower(),
            "role": payload.role,
            "invited_by": user.id,
        },
    )
    if not invite:
        raise HTTPException(status_code=400, detail="Failed to create invite")
    return {"invite": invite, "invite_url": f"/invite/{invite['token']}"}


@router.patch("/organization-admins/{member_id}")
def update_organization_admin(
    member_id: str,
    payload: UpdateOrgAdminRequest,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    updated = rest_patch("organization_members", {"role": payload.role}, match={"id": member_id})
    if not updated:
        raise HTTPException(status_code=404, detail="Member not found")
    return updated


@router.delete("/organization-admins/{member_id}")
def delete_organization_admin(
    member_id: str,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, str]:
    deleted = rest_delete("organization_members", match={"id": member_id})
    if not deleted:
        raise HTTPException(status_code=404, detail="Member not found")
    return {"status": "deleted"}


@router.get("/me")
def get_me(user: Annotated[AuthUser, Depends(require_auth)]) -> dict[str, Any]:
    if user.role == "super_admin":
        organizations = rest_get(
            "organizations",
            params={"select": "id,name", "order": "name.asc"},
        )
    elif user.organization_ids:
        organizations = rest_get(
            "organizations",
            params={
                "id": f"in.({','.join(user.organization_ids)})",
                "select": "id,name",
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
        "organizations": organizations,
    }
