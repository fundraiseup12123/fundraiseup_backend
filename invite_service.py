from __future__ import annotations

import logging
import os
import secrets
import string
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException

from db import rest_get_one, rest_insert, rest_patch, supabase_url
from emails import log_email, send_resend_email
from email_branding import DEFAULT_EMAIL_BANNER_URL, DEFAULT_EMAIL_LOGO_URL, DEFAULT_PRIMARY_COLOR
from email_templates import org_admin_invite_email
from frontend_url import resolve_frontend_url, resolve_invite_frontend_url

logger = logging.getLogger(__name__)


def _supabase_secret() -> str:
    return os.getenv("SUPABASE_SECRET_KEY", "").strip()


def _admin_headers() -> dict[str, str]:
    secret = _supabase_secret()
    return {
        "apikey": secret,
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
    }


def generate_temp_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def get_user_email_by_id(user_id: str) -> str | None:
    url = supabase_url()
    if not url or not _supabase_secret():
        return None
    try:
        response = httpx.get(
            f"{url}/auth/v1/admin/users/{user_id}",
            headers=_admin_headers(),
            timeout=20.0,
        )
        if response.status_code == 200:
            email = response.json().get("email")
            return str(email).strip().lower() if email else None
    except httpx.HTTPError as exc:
        logger.warning("Supabase user lookup failed for %s: %s", user_id, exc)
    return None


def find_user_id_by_email(email: str) -> str | None:
    """Return Auth user id only when the email matches exactly (case-insensitive).

    Prefer GoTrue ``filter`` (email/phone search). Fall back to a short page scan
    with exact matching — never return ``users[0]`` without comparing email.
    """
    url = supabase_url()
    if not url or not _supabase_secret():
        return None
    target = email.strip().lower()
    if not target:
        return None

    def _match(users: list[Any]) -> str | None:
        for user in users:
            if not isinstance(user, dict):
                continue
            if str(user.get("email") or "").strip().lower() == target:
                user_id = str(user.get("id") or "").strip()
                return user_id or None
        return None

    try:
        # Fast path: filter query (partial search — still require exact match).
        response = httpx.get(
            f"{url}/auth/v1/admin/users",
            headers=_admin_headers(),
            params={"page": 1, "per_page": 50, "filter": target},
            timeout=8.0,
        )
        if response.status_code == 200:
            payload = response.json()
            users = payload.get("users") if isinstance(payload, dict) else payload
            if isinstance(users, list):
                matched = _match(users)
                if matched:
                    return matched

        # Slow path: limited pages only (signup/login checks — not email sends).
        page = 1
        per_page = 200
        while page <= 5:
            response = httpx.get(
                f"{url}/auth/v1/admin/users",
                headers=_admin_headers(),
                params={"page": page, "per_page": per_page},
                timeout=8.0,
            )
            if response.status_code != 200:
                return None
            payload = response.json()
            users = payload.get("users") if isinstance(payload, dict) else payload
            if not isinstance(users, list) or not users:
                return None
            matched = _match(users)
            if matched:
                return matched
            if len(users) < per_page:
                return None
            page += 1
    except httpx.HTTPError as exc:
        logger.warning("Supabase user lookup failed for %s: %s", email, exc)
    return None



def create_supabase_user(
    email: str,
    password: str,
    *,
    first_name: str = "",
    last_name: str = "",
) -> str:
    url = supabase_url()
    if not url or not _supabase_secret():
        raise HTTPException(status_code=503, detail="Supabase admin API is not configured")

    response = httpx.post(
        f"{url}/auth/v1/admin/users",
        headers=_admin_headers(),
        json={
            "email": email.lower(),
            "password": password,
            "email_confirm": True,
            "user_metadata": {
                "first_name": first_name,
                "last_name": last_name,
            },
        },
        timeout=20.0,
    )
    if response.status_code in {200, 201}:
        data = response.json()
        user_id = data.get("id") or (data.get("user") or {}).get("id")
        if user_id:
            return str(user_id)

    if response.status_code == 422:
        existing_id = find_user_id_by_email(email)
        if existing_id:
            return existing_id

    detail = "Unable to create admin account"
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("msg"):
            detail = str(body["msg"])
        elif isinstance(body, dict) and body.get("message"):
            detail = str(body["message"])
    except Exception:
        pass
    raise HTTPException(status_code=400, detail=detail)


def ensure_org_member(user_id: str, organization_id: str, role: str) -> None:
    existing = rest_get_one(
        "organization_members",
        params={
            "organization_id": f"eq.{organization_id}",
            "user_id": f"eq.{user_id}",
            "select": "id,role",
        },
    )
    if existing:
        if existing.get("role") != role:
            rest_patch(
                "organization_members",
                {"role": role},
                match={"id": existing["id"]},
            )
        return
    inserted = rest_insert(
        "organization_members",
        {
            "organization_id": organization_id,
            "user_id": user_id,
            "role": role,
        },
    )
    if not inserted:
        raise HTTPException(status_code=400, detail="Failed to add user to organization")


def mark_invite_accepted(invite_id: str) -> None:
    rest_patch(
        "organization_invites",
        {"accepted_at": datetime.now(timezone.utc).isoformat()},
        match={"id": invite_id},
    )


def update_supabase_user_password(user_id: str, password: str) -> None:
    url = supabase_url()
    if not url or not _supabase_secret():
        raise HTTPException(status_code=503, detail="Supabase admin API is not configured")
    response = httpx.put(
        f"{url}/auth/v1/admin/users/{user_id}",
        headers=_admin_headers(),
        json={"password": password},
        timeout=20.0,
    )
    if response.status_code not in {200, 201}:
        raise HTTPException(status_code=400, detail="Unable to set account password")


def send_pending_organization_invite(
    invite: dict[str, Any],
    *,
    organization_name: str,
) -> dict[str, Any]:
    """Email invite link; account is created when the invitee sets a password."""
    if invite.get("accepted_at"):
        raise HTTPException(status_code=400, detail="Invite already accepted")

    email = str(invite.get("email") or "").lower()
    org_id = str(invite.get("organization_id") or "")
    role = str(invite.get("role") or "member")
    token = str(invite.get("token") or "").strip()
    if not email or not org_id or not token:
        raise HTTPException(status_code=400, detail="Invalid invite")

    invite_url = f"{resolve_invite_frontend_url().rstrip('/')}/invite/{token}"
    existing_user = bool(find_user_id_by_email(email))
    email_result = send_org_invite_email(
        recipient_email=email,
        organization_id=org_id,
        organization_name=organization_name,
        role=role,
        login_url=invite_url,
        temporary_password=None,
        existing_user=existing_user,
    )
    return {
        "email_sent": bool(email_result.get("sent")),
        "email_configured": email_result.get("sent") is not False
        or email_result.get("reason") != "not_configured",
        "existing_user": existing_user,
        "invite_url": invite_url,
    }


def send_org_invite_email(
    *,
    recipient_email: str,
    organization_id: str,
    organization_name: str,
    role: str,
    login_url: str,
    temporary_password: str | None,
    existing_user: bool,
) -> dict[str, Any]:
    subject, html = org_admin_invite_email(
        organization_name=organization_name,
        role=role,
        login_url=login_url,
        email=recipient_email,
        temporary_password=temporary_password,
        existing_user=existing_user,
        logo_url=os.getenv("EMAIL_LOGO_URL", "").strip() or DEFAULT_EMAIL_LOGO_URL,
        primary_color=DEFAULT_PRIMARY_COLOR,
        banner_url=os.getenv("EMAIL_BANNER_URL", "").strip() or DEFAULT_EMAIL_BANNER_URL,
        contact_email=None,
    )
    result = send_resend_email(to=recipient_email, subject=subject, html=html)
    if result.get("sent"):
        log_email(
            recipient_email=recipient_email,
            subject=subject,
            template_key="org_admin_invite",
            organization_id=organization_id,
        )
    return result


def fulfill_organization_invite(
    invite: dict[str, Any],
    *,
    organization_name: str,
    first_name: str = "",
    last_name: str = "",
) -> dict[str, Any]:
    if invite.get("accepted_at"):
        raise HTTPException(status_code=400, detail="Invite already accepted")

    email = str(invite.get("email") or "").lower()
    org_id = str(invite.get("organization_id") or "")
    role = str(invite.get("role") or "admin")
    if not email or not org_id:
        raise HTTPException(status_code=400, detail="Invalid invite")

    existing_user_id = find_user_id_by_email(email)
    login_url = f"{resolve_invite_frontend_url().rstrip('/')}/login?next=/admin/insights"
    password: str | None = None
    created_new = False

    if existing_user_id:
        user_id = existing_user_id
    else:
        password = generate_temp_password()
        user_id = create_supabase_user(email, password, first_name=first_name, last_name=last_name)
        created_new = True

    ensure_org_member(user_id, org_id, role)
    if first_name or last_name:
        rest_patch(
            "profiles",
            {"first_name": first_name, "last_name": last_name},
            match={"id": user_id},
        )
    mark_invite_accepted(str(invite["id"]))

    email_result = send_org_invite_email(
        recipient_email=email,
        organization_id=org_id,
        organization_name=organization_name,
        role=role,
        login_url=login_url,
        temporary_password=password,
        existing_user=not created_new,
    )

    return {
        "user_id": user_id,
        "email_sent": bool(email_result.get("sent")),
        "email_configured": email_result.get("sent") is not False or email_result.get("reason") != "not_configured",
        "existing_user": not created_new,
        "login_url": login_url,
    }
