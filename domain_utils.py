from __future__ import annotations

import os
import re
from urllib.parse import urlparse

# Railway default hostnames only have a cert for the exact service hostname — not gaza.*.up.railway.app
_UNSUPPORTED_ROOT_SUFFIXES = (
    ".up.railway.app",
    ".railway.app",
    ".localhost",
    ".local",
)


def is_valid_platform_root_domain(host: str) -> bool:
    host = host.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0].rstrip(".")
    if not host or host in {"localhost", "127.0.0.1"}:
        return False
    if host.count(".") < 1:
        return False
    return not any(host.endswith(suffix) for suffix in _UNSUPPORTED_ROOT_SUFFIXES)


def platform_root_domain() -> str:
    explicit = os.getenv("PLATFORM_ROOT_DOMAIN", "").strip().lower()
    if explicit:
        cleaned = explicit.removeprefix("https://").removeprefix("http://").split("/")[0].rstrip(".")
        if is_valid_platform_root_domain(cleaned):
            return cleaned
        return ""

    frontend = os.getenv("FRONTEND_URL", "").strip()
    if frontend:
        host = urlparse(frontend if "://" in frontend else f"https://{frontend}").hostname
        if host and is_valid_platform_root_domain(host):
            return host.lower()

    return ""


def platform_subdomain_warning() -> str | None:
    explicit = os.getenv("PLATFORM_ROOT_DOMAIN", "").strip().lower()
    if explicit:
        cleaned = explicit.removeprefix("https://").removeprefix("http://").split("/")[0].rstrip(".")
        if not is_valid_platform_root_domain(cleaned):
            return (
                f"PLATFORM_ROOT_DOMAIN is set to {cleaned}, but Railway hostnames like *.up.railway.app "
                "cannot be used for campaign subdomains — they do not get SSL certificates. "
                "Use a custom domain you own (e.g. fundraiseup.com), add *.yourdomain.com in Railway → Domains, "
                "then set PLATFORM_ROOT_DOMAIN=yourdomain.com."
            )
    if not platform_root_domain():
        return (
            "Set PLATFORM_ROOT_DOMAIN to a custom domain you own (not a Railway *.up.railway.app hostname). "
            "Add wildcard DNS *.yourdomain.com and register *.yourdomain.com in Railway → Domains."
        )
    return None


def platform_domain_config() -> dict[str, str | bool | None]:
    root = platform_root_domain()
    return {
        "platform_root_domain": root,
        "supports_subdomains": bool(root),
        "subdomain_warning": platform_subdomain_warning(),
    }


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

    if any(hostname.endswith(suffix) for suffix in _UNSUPPORTED_ROOT_SUFFIXES):
        raise ValueError(
            "Subdomains on Railway *.up.railway.app hostnames are not supported — browsers will show SSL errors. "
            "Use a custom domain you own and set PLATFORM_ROOT_DOMAIN on the backend."
        )

    root = platform_root_domain()
    if "." not in hostname:
        if not root:
            warning = platform_subdomain_warning()
            raise ValueError(warning or "Enter a full custom domain (e.g. donate.example.org).")
        label = normalize_subdomain_label(hostname)
        return f"{label}.{root}", True

    return hostname, bool(root and hostname.endswith(f".{root}"))


def subdomain_label_from_hostname(hostname: str) -> str | None:
    root = platform_root_domain()
    if not root or not hostname.endswith(f".{root}"):
        return None
    label = hostname[: -(len(root) + 1)]
    return label or None
