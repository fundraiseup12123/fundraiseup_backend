from __future__ import annotations

import os


def resolve_frontend_url(origin: str | None = None) -> str:
    """Prefer explicit origin from the browser, then FRONTEND_URL env."""
    if origin:
        cleaned = origin.strip().rstrip("/")
        if cleaned.startswith("http://") or cleaned.startswith("https://"):
            return cleaned

    env_url = os.getenv("FRONTEND_URL", "").strip().rstrip("/")
    if env_url:
        return env_url

    return "http://localhost:3000"
