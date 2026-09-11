"""Platform-wide All Campaigns Directory router.

Provides aggregated organizations, campaigns, landing/popup metadata,
role-based tab access management, and collaborative notes visible to all.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import AuthUser, require_auth, require_super_admin
from db import _headers, rest_delete, rest_get, rest_insert, supabase_enabled, supabase_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/super/campaign-directory", tags=["platform-campaign-directory"])

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NOTES_STORE_PATH = DATA_DIR / "campaign_notes_store.json"
ROLE_ACCESS_PATH = DATA_DIR / "campaign_directory_role_access.json"

DEFAULT_ALLOWED_ROLES = ["super_admin", "platform_admin", "admin", "member"]

_DB_CHECK_CACHE: dict[str, Any] = {"checked_at": 0.0, "exists": False}


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _supabase_notes_table_exists() -> bool:
    now = time.time()
    if now - _DB_CHECK_CACHE["checked_at"] < 30.0:
        return bool(_DB_CHECK_CACHE["exists"])

    if not supabase_enabled():
        _DB_CHECK_CACHE["checked_at"] = now
        _DB_CHECK_CACHE["exists"] = False
        return False

    try:
        r = httpx.get(
            f"{supabase_url()}/rest/v1/campaign_notes",
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


def _read_local_notes() -> list[dict[str, Any]]:
    _ensure_data_dir()
    if not NOTES_STORE_PATH.exists():
        return []
    try:
        with open(NOTES_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("Failed to read local campaign notes store: %s", exc)
        return []


def _write_local_notes(notes: list[dict[str, Any]]) -> None:
    _ensure_data_dir()
    try:
        with open(NOTES_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(notes, f, indent=2)
    except Exception as exc:
        logger.error("Failed to write local campaign notes store: %s", exc)


def _read_role_access() -> list[str]:
    _ensure_data_dir()
    if not ROLE_ACCESS_PATH.exists():
        return list(DEFAULT_ALLOWED_ROLES)
    try:
        with open(ROLE_ACCESS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list) and data:
                return data
            if isinstance(data, dict) and "allowed_roles" in data:
                return list(data["allowed_roles"])
    except Exception as exc:
        logger.warning("Failed to read role access store: %s", exc)
    return list(DEFAULT_ALLOWED_ROLES)


def _write_role_access(roles: list[str]) -> None:
    _ensure_data_dir()
    cleaned = list(dict.fromkeys(r.strip().lower() for r in roles if r and r.strip()))
    if "super_admin" not in cleaned:
        cleaned.insert(0, "super_admin")
    try:
        with open(ROLE_ACCESS_PATH, "w", encoding="utf-8") as f:
            json.dump({"allowed_roles": cleaned, "updated_at": datetime.now(timezone.utc).isoformat()}, f, indent=2)
    except Exception as exc:
        logger.error("Failed to write role access store: %s", exc)


def _verify_tab_access(user: AuthUser) -> None:
    """Verifies that the user has a permitted role or super_admin status."""
    if user.role == "super_admin":
        return

    allowed = _read_role_access()
    # Check top-level role
    if user.role in allowed:
        return

    # Check org_roles if member has admin role inside an org
    user_roles = set(user.org_roles.values()) if user.org_roles else set()
    if any(r in allowed for r in user_roles):
        return

    raise HTTPException(
        status_code=403,
        detail="You do not have permission to access the Platform Campaigns Directory.",
    )


# ---------------------------------------------------------------------------
# Pydantic Request & Response Models
# ---------------------------------------------------------------------------

class CreateNoteRequest(BaseModel):
    campaign_id: str
    organization_id: str | None = None
    content: str = Field(min_length=1, max_length=5000)


class UpdateRoleAccessRequest(BaseModel):
    allowed_roles: list[str] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/role-access")
def get_role_access(
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    """Returns currently allowed roles for the Platform Campaigns tab."""
    allowed = _read_role_access()
    return {
        "allowed_roles": allowed,
        "user_role": user.role,
        "can_manage": user.role == "super_admin",
        "has_access": user.role == "super_admin" or user.role in allowed or bool(set(user.org_roles.values()) & set(allowed)),
    }


@router.post("/role-access")
def update_role_access(
    payload: UpdateRoleAccessRequest,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    """Super Admin configures which roles can access this Platform tab."""
    _write_role_access(payload.allowed_roles)
    return {
        "allowed_roles": _read_role_access(),
        "message": "Role access updated successfully.",
    }


@router.get("/data")
def get_campaign_directory_data(
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    """Fetch all organizations one by one, each with its campaigns, titles, dates, URLs, and notes count."""
    _verify_tab_access(user)

    # 1. Fetch organizations
    org_rows = rest_get(
        "organizations",
        params={"select": "id,name,slug,status,default_currency,created_at", "order": "name.asc", "limit": "500"},
    )

    # 2. Fetch all campaigns
    campaign_rows = rest_get(
        "campaigns",
        params={
            "select": "id,organization_id,name,slug,status,default_currency,designation,created_at,updated_at",
            "order": "created_at.desc",
            "limit": "2000",
        },
    )

    # 3. Fetch campaign content titles for public header text
    content_rows = rest_get(
        "campaign_content",
        params={"select": "campaign_id,title", "limit": "2000"},
    )
    title_by_campaign = {
        str(c.get("campaign_id")): str(c.get("title") or "").strip()
        for c in content_rows
        if c.get("campaign_id")
    }

    # 4. Fetch notes to count them per campaign
    notes: list[dict[str, Any]] = []
    if _supabase_notes_table_exists():
        notes = rest_get("campaign_notes", params={"select": "id,campaign_id", "limit": "5000"})
    else:
        notes = _read_local_notes()

    notes_count_map: dict[str, int] = {}
    for n in notes:
        cid = str(n.get("campaign_id") or "")
        if cid:
            notes_count_map[cid] = notes_count_map.get(cid, 0) + 1

    # 5. Group campaigns by organization_id
    campaigns_by_org: dict[str, list[dict[str, Any]]] = {}
    for c in campaign_rows:
        oid = str(c.get("organization_id") or "")
        cid = str(c.get("id") or "")
        c_name = str(c.get("name") or "Untitled Campaign")
        c_title = title_by_campaign.get(cid) or c_name
        slug = str(c.get("slug") or "")

        campaign_item = {
            "id": cid,
            "organization_id": oid,
            "name": c_name,
            "title": c_title,
            "slug": slug,
            "status": str(c.get("status") or "active"),
            "default_currency": str(c.get("default_currency") or "USD"),
            "designation": c.get("designation") or "General designation",
            "created_at": c.get("created_at") or datetime.now(timezone.utc).isoformat(),
            "updated_at": c.get("updated_at") or c.get("created_at"),
            "notes_count": notes_count_map.get(cid, 0),
        }
        campaigns_by_org.setdefault(oid, []).append(campaign_item)

    # 6. Build structured organization response
    org_list: list[dict[str, Any]] = []
    total_campaigns_count = 0
    total_active_campaigns_count = 0

    for o in org_rows:
        oid = str(o.get("id") or "")
        org_campaigns = campaigns_by_org.get(oid, [])
        total_campaigns_count += len(org_campaigns)
        active_count = sum(
            1 for cp in org_campaigns
            if str(cp.get("status") or "").lower() in ("live", "active")
        )
        total_active_campaigns_count += active_count

        org_list.append({
            "id": oid,
            "name": str(o.get("name") or "Organization"),
            "slug": str(o.get("slug") or ""),
            "status": str(o.get("status") or "active"),
            "default_currency": str(o.get("default_currency") or "USD"),
            "created_at": o.get("created_at"),
            "campaigns_count": len(org_campaigns),
            "active_campaigns_count": active_count,
            "campaigns": org_campaigns,
        })

    allowed_roles = _read_role_access()

    return {
        "organizations": org_list,
        "summary": {
            "total_organizations": len(org_list),
            "total_campaigns": total_campaigns_count,
            "total_active_campaigns": total_active_campaigns_count,
            "total_notes": sum(notes_count_map.values()),
        },
        "allowed_roles": allowed_roles,
        "user_permissions": {
            "role": user.role,
            "is_super_admin": user.role == "super_admin",
            "can_manage_roles": user.role == "super_admin",
        },
    }


@router.get("/notes")
def list_campaign_notes(
    campaign_id: str | None = None,
    user: Annotated[AuthUser, Depends(require_auth)] = None,
) -> list[dict[str, Any]]:
    """List all notes or filter by campaign_id. Sorted newest first. Visible to all permitted users."""
    if user:
        _verify_tab_access(user)

    if _supabase_notes_table_exists():
        params: dict[str, str] = {
            "select": "id,campaign_id,organization_id,author_name,author_email,author_role,content,created_at",
            "order": "created_at.desc",
            "limit": "2000",
        }
        if campaign_id:
            params["campaign_id"] = f"eq.{campaign_id}"
        notes = rest_get("campaign_notes", params=params)
        return notes

    # Fallback to local store
    local_notes = _read_local_notes()
    if campaign_id:
        local_notes = [n for n in local_notes if str(n.get("campaign_id")) == campaign_id]
    local_notes.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return local_notes


@router.post("/notes")
def create_campaign_note(
    payload: CreateNoteRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    """Add a note to a campaign. Stored persistently and visible to all permitted users."""
    _verify_tab_access(user)

    fname = str(user.first_name or "").strip()
    lname = str(user.last_name or "").strip()
    name = f"{fname} {lname}".strip() or user.email.split("@")[0].capitalize()
    role = user.role or "member"

    note_data = {
        "id": str(uuid.uuid4()),
        "campaign_id": payload.campaign_id,
        "organization_id": payload.organization_id,
        "author_name": name,
        "author_email": user.email,
        "author_role": role,
        "content": payload.content.strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    if _supabase_notes_table_exists():
        inserted = rest_insert("campaign_notes", [note_data])
        if inserted and isinstance(inserted, list) and inserted[0]:
            return inserted[0]
        # If rest_insert succeeded without returning row, return note_data
        return note_data

    # Fallback: persist in local JSON store
    local_notes = _read_local_notes()
    local_notes.append(note_data)
    _write_local_notes(local_notes)
    return note_data


@router.delete("/notes/{note_id}")
def delete_campaign_note(
    note_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    """Delete a note. Allowed for the note's author or super admin."""
    _verify_tab_access(user)

    if _supabase_notes_table_exists():
        # Check author if not super admin
        if user.role != "super_admin":
            existing = rest_get("campaign_notes", params={"id": f"eq.{note_id}", "limit": "1"})
            if existing and existing[0].get("author_email", "").lower() != user.email.lower():
                raise HTTPException(status_code=403, detail="You can only delete your own notes.")
        rest_delete("campaign_notes", params={"id": f"eq.{note_id}"})
        return {"deleted": True, "note_id": note_id}

    local_notes = _read_local_notes()
    idx = next((i for i, n in enumerate(local_notes) if str(n.get("id")) == note_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Note not found")

    note = local_notes[idx]
    if user.role != "super_admin" and str(note.get("author_email", "")).lower() != user.email.lower():
        raise HTTPException(status_code=403, detail="You can only delete your own notes.")

    local_notes.pop(idx)
    _write_local_notes(local_notes)
    return {"deleted": True, "note_id": note_id}
