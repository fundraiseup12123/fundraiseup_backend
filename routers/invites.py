from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from auth import AuthUser, require_auth
from db import rest_get_one, rest_insert, rest_patch

router = APIRouter(prefix="/invites", tags=["invites"])


class AcceptInviteRequest(BaseModel):
    token: str
    password: str
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
    return invite


@router.post("/{token}/accept")
def accept_invite(token: str, payload: AcceptInviteRequest) -> dict[str, str]:
    invite = rest_get_one(
        "organization_invites",
        params={"token": f"eq.{token}", "select": "*"},
    )
    if not invite or invite.get("accepted_at"):
        raise HTTPException(status_code=400, detail="Invalid invite")

    import httpx
    import os
    from pathlib import Path
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    secret = os.getenv("SUPABASE_SECRET_KEY", "")

    signup = httpx.post(
        f"{url}/auth/v1/signup",
        headers={"apikey": secret, "Content-Type": "application/json"},
        json={
            "email": invite["email"],
            "password": payload.password,
            "data": {"first_name": payload.first_name, "last_name": payload.last_name},
        },
        timeout=20.0,
    )
    if signup.status_code not in {200, 201}:
        signin = httpx.post(
            f"{url}/auth/v1/token?grant_type=password",
            headers={"apikey": secret, "Content-Type": "application/json"},
            json={"email": invite["email"], "password": payload.password},
            timeout=20.0,
        )
        if signin.status_code != 200:
            raise HTTPException(status_code=400, detail="Unable to create account")
        user_id = signin.json().get("user", {}).get("id")
    else:
        user_id = signup.json().get("user", {}).get("id") or signup.json().get("id")

    if not user_id:
        raise HTTPException(status_code=400, detail="Unable to resolve user")

    rest_insert(
        "organization_members",
        {
            "organization_id": invite["organization_id"],
            "user_id": user_id,
            "role": invite["role"],
        },
    )
    rest_patch(
        "organization_invites",
        {"accepted_at": datetime.now(timezone.utc).isoformat()},
        match={"id": invite["id"]},
    )
    rest_patch(
        "profiles",
        {"first_name": payload.first_name, "last_name": payload.last_name},
        match={"id": user_id},
    )
    return {"status": "accepted", "organization_id": invite["organization_id"]}
