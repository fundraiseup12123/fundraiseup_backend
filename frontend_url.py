from __future__ import annotations

import base64
import os


def resolve_frontend_url(origin: str | None = None) -> str:
    """Prefer explicit origin from the browser, then FRONTEND_URL env."""
    if origin:
        cleaned = origin.strip().strip('"').strip("'").rstrip("/")
        if cleaned.startswith("http://") or cleaned.startswith("https://"):
            return cleaned

    env_url = os.getenv("FRONTEND_URL", "").strip().strip('"').strip("'").rstrip("/")
    if env_url:
        return env_url

    return "http://localhost:3000"


def resolve_invite_frontend_url(origin: str | None = None) -> str:
    """Public invite links: rewrite mistaken .app hosts to .com."""
    from urllib.parse import urlparse, urlunparse

    base = resolve_frontend_url(origin)
    try:
        parsed = urlparse(base)
        host = (parsed.hostname or "").lower()
        if host.endswith(".app") and "railway.app" not in host:
            netloc = parsed.netloc
            if netloc.lower().endswith(".app"):
                netloc = netloc[: -len(".app")] + ".com"
            return urlunparse(
                (parsed.scheme, netloc, parsed.path or "", "", "", "")
            ).rstrip("/")
    except Exception:
        pass
    return base


def pack_state_token(value: str) -> str:
    """Encode arbitrary text for OAuth state (URL-safe, no colons)."""
    raw = (value or "").encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unpack_state_token(token: str | None) -> str | None:
    if not token:
        return None
    padded = token + "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return None


def pack_origin_token(frontend_url: str) -> str:
    """Encode a frontend origin for OAuth state (URL-safe, no colons)."""
    return pack_state_token(resolve_frontend_url(frontend_url))


def unpack_origin_token(token: str | None) -> str | None:
    decoded = unpack_state_token(token)
    if not decoded:
        return None
    if decoded.startswith("http://") or decoded.startswith("https://"):
        return decoded.rstrip("/")
    return None
