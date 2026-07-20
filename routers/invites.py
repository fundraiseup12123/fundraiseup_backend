from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from db import rest_get_one, rest_patch
from invite_service import (
    create_supabase_user,
    ensure_org_member,
    find_user_id_by_email,
    mark_invite_accepted,
    update_supabase_user_password,
)

router = APIRouter(prefix="/invites", tags=["invites"])


class AcceptInviteRequest(BaseModel):
    token: str
    password: str = Field(min_length=8)
    first_name: str = ""
    last_name: str = ""


@router.get("/{token}")
def get_invite(token: str) -> dict[str, Any]:
    invite = rest_get_one(
        "organization_invites",
        params={
            "token": f"eq.{token}",
            "select": "id,email,role,organization_id,expires_at,accepted_at,organizations(id,name,slug)",
        },
    )
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")
    if invite.get("accepted_at"):
        raise HTTPException(status_code=400, detail="Invite already accepted")
    expires_at = invite.get("expires_at")
    if expires_at:
        try:
            raw = str(expires_at).replace("Z", "+00:00")
            if datetime.fromisoformat(raw) < datetime.now(timezone.utc):
                raise HTTPException(status_code=400, detail="Invite has expired")
        except ValueError:
            pass
    return invite


@router.post("/{token}/accept")
def accept_invite(token: str, payload: AcceptInviteRequest) -> dict[str, str]:
    if payload.token != token:
        raise HTTPException(status_code=400, detail="Invalid invite")
    if len(payload.password or "") < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    invite = rest_get_one(
        "organization_invites",
        params={"token": f"eq.{token}", "select": "*"},
    )
    if not invite or invite.get("accepted_at"):
        raise HTTPException(status_code=400, detail="Invalid invite")

    email = str(invite.get("email") or "").strip().lower()
    org_id = str(invite.get("organization_id") or "")
    role = str(invite.get("role") or "member")
    if not email or not org_id:
        raise HTTPException(status_code=400, detail="Invalid invite")

    first_name = (payload.first_name or "").strip()
    last_name = (payload.last_name or "").strip()
    existing_user_id = find_user_id_by_email(email)
    if existing_user_id:
        user_id = existing_user_id
        update_supabase_user_password(user_id, payload.password)
    else:
        user_id = create_supabase_user(
            email,
            payload.password,
            first_name=first_name,
            last_name=last_name,
        )

    ensure_org_member(user_id, org_id, role)
    if first_name or last_name:
        rest_patch(
            "profiles",
            {"first_name": first_name, "last_name": last_name},
            match={"id": user_id},
        )
    mark_invite_accepted(str(invite["id"]))
    return {"status": "accepted", "organization_id": org_id}
