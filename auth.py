from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Header
from pathlib import Path
from dotenv import load_dotenv

from db import rest_get_one, supabase_url

load_dotenv(Path(__file__).resolve().parent / ".env")

SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")


@dataclass
class AuthUser:
    id: str
    email: str
    role: str
    first_name: str | None
    last_name: str | None
    organization_ids: list[str]
    org_roles: dict[str, str]


def _supabase_api_key() -> str:
    return (
        os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
        or os.getenv("SUPABASE_ANON_KEY", "")
        or os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY", "")
        or os.getenv("SUPABASE_SECRET_KEY", "")
    )


def _decode_via_supabase(token: str) -> dict:
    url = supabase_url()
    if not url:
        raise HTTPException(status_code=503, detail="Auth not configured")
    api_key = _supabase_api_key()
    if not api_key:
        raise HTTPException(status_code=503, detail="Supabase API key not configured")
    response = httpx.get(
        f"{url}/auth/v1/user",
        headers={
            "apikey": api_key,
            "Authorization": f"Bearer {token}",
        },
        timeout=15.0,
    )
    if response.status_code != 200:
        detail = "Invalid or expired token"
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("msg"):
                detail = str(body["msg"])
        except Exception:
            pass
        raise HTTPException(status_code=401, detail=detail)
    return response.json()


def get_user_from_token(token: str) -> AuthUser:
    user_data = _decode_via_supabase(token)
    user_id = user_data.get("id")
    email = user_data.get("email", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    profile = rest_get_one(
        "profiles",
        params={"select": "id,role,first_name,last_name", "id": f"eq.{user_id}"},
    ) or {"role": "org_user", "first_name": "", "last_name": ""}

    members = []
    if supabase_url():
        import httpx as hx
        from db import _headers
        resp = hx.get(
            f"{supabase_url()}/rest/v1/organization_members",
            headers=_headers(user_jwt=token),
            params={"select": "organization_id,role", "user_id": f"eq.{user_id}"},
            timeout=15.0,
        )
        if resp.status_code == 200:
            members = resp.json() if isinstance(resp.json(), list) else []

    org_ids = [m["organization_id"] for m in members]
    org_roles = {m["organization_id"]: m["role"] for m in members}

    return AuthUser(
        id=user_id,
        email=email,
        role=profile.get("role", "org_user"),
        first_name=profile.get("first_name"),
        last_name=profile.get("last_name"),
        organization_ids=org_ids,
        org_roles=org_roles,
    )


def require_auth(authorization: Annotated[str | None, Header()] = None) -> AuthUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    return get_user_from_token(token)


def require_super_admin(user: Annotated[AuthUser, Depends(require_auth)]) -> AuthUser:
    if user.role != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin access required")
    return user


def require_org_access(org_id: str, user: AuthUser, min_role: str = "member") -> None:
    if user.role == "super_admin":
        return
    if org_id not in user.organization_ids:
        raise HTTPException(status_code=403, detail="Organization access denied")
    role = user.org_roles.get(org_id, "member")
    hierarchy = {"member": 0, "admin": 1, "owner": 2}
    if hierarchy.get(role, 0) < hierarchy.get(min_role, 0):
        raise HTTPException(status_code=403, detail="Insufficient organization permissions")
