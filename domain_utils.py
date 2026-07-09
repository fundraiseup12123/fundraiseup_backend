from __future__ import annotations

import os
import re
from urllib.parse import urlparse


def platform_root_domain() -> str:
    explicit = os.getenv("PLATFORM_ROOT_DOMAIN", "").strip().lower()
    if explicit:
        return explicit.removeprefix("https://").removeprefix("http://").split("/")[0].rstrip(".")

    frontend = os.getenv("FRONTEND_URL", "").strip()
    if frontend and ".up.railway.app" not in frontend:
        host = urlparse(frontend if "://" in frontend else f"https://{frontend}").hostname
        if host and host not in {"localhost", "127.0.0.1"}:
            return host.lower()

    return ""


def normalize_subdomain_label(value: str) -> str:
    label = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label):
        raise ValueError("Subdomain can only use letters, numbers, and hyphens.")
    return label


def resolve_campaign_hostname(raw: str) -> tuple[str, bool]:
    """Return (hostname, is_platform_subdomain)."""
    hostname = raw.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0].rstrip(".")
    if not hostname:
        raise ValueError("Enter a subdomain or full domain.")

    root = platform_root_domain()
    if "." not in hostname:
        if not root:
            raise ValueError(
                "Enter a full domain (e.g. donate.example.org) or ask your admin to set PLATFORM_ROOT_DOMAIN."
            )
        label = normalize_subdomain_label(hostname)
        return f"{label}.{root}", True

    return hostname, bool(root and hostname.endswith(f".{root}"))
