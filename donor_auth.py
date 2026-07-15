"""Signed magic-link + session tokens for the donor portal (no password)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from fastapi import Header, HTTPException


def _secret() -> str:
    secret = (
        os.getenv("DONOR_PORTAL_SECRET", "").strip()
        or os.getenv("SUPABASE_JWT_SECRET", "").strip()
        or os.getenv("SUPABASE_SECRET_KEY", "").strip()
        or os.getenv("STRIPE_SECRET_KEY", "").strip()
    )
    if not secret:
        raise HTTPException(status_code=503, detail="Donor portal auth is not configured")
    return secret


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def sign_token(payload: dict[str, Any]) -> str:
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = hmac.new(_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def verify_token(token: str, *, purpose: str | None = None) -> dict[str, Any]:
    try:
        body, sig = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    expected = hmac.new(_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    try:
        provided = _b64url_decode(sig)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="Invalid token")

    try:
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=401, detail="Invalid token")

    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or time.time() > float(exp):
        raise HTTPException(status_code=401, detail="Link expired. Request a new one.")

    if purpose and payload.get("purpose") != purpose:
        raise HTTPException(status_code=401, detail="Invalid token")

    email = str(payload.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=401, detail="Invalid token")

    payload["email"] = email
    return payload


def issue_magic_link_token(email: str, *, ttl_seconds: int = 60 * 30) -> str:
    now = int(time.time())
    return sign_token(
        {
            "email": email.strip().lower(),
            "purpose": "magic_link",
            "iat": now,
            "exp": now + ttl_seconds,
        }
    )


def issue_session_token(email: str, *, ttl_seconds: int = 60 * 60 * 24 * 14) -> str:
    now = int(time.time())
    return sign_token(
        {
            "email": email.strip().lower(),
            "purpose": "session",
            "iat": now,
            "exp": now + ttl_seconds,
        }
    )


def require_donor_email(
    authorization: str | None = Header(default=None),
) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in required")
    token = authorization.split(" ", 1)[1].strip()
    payload = verify_token(token, purpose="session")
    return str(payload["email"])
