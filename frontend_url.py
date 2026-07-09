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


def pack_origin_token(frontend_url: str) -> str:
    """Encode a frontend origin for OAuth state (URL-safe, no colons)."""
    raw = resolve_frontend_url(frontend_url).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def unpack_origin_token(token: str | None) -> str | None:
    if not token:
        return None
    padded = token + "=" * (-len(token) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    if decoded.startswith("http://") or decoded.startswith("https://"):
        return decoded.rstrip("/")
    return None
