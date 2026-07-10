from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx

BUCKET = "campaign-assets"


def _supabase_url() -> str:
    return os.getenv("SUPABASE_URL", "").rstrip("/")


def _supabase_secret() -> str:
    return os.getenv("SUPABASE_SECRET_KEY", "")


def supabase_storage_configured() -> bool:
    return bool(_supabase_url() and _supabase_secret())


def upload_campaign_asset(buffer: bytes, filename: str, content_type: str) -> str:
    base_url = _supabase_url()
    secret = _supabase_secret()
    if not base_url or not secret:
        raise RuntimeError("Supabase storage is not configured on the server.")

    ensure_campaign_assets_bucket()

    ext = Path(filename).suffix or ""
    object_path = f"campaigns/{uuid.uuid4()}{ext}"

    response = httpx.post(
        f"{base_url}/storage/v1/object/{BUCKET}/{object_path}",
        headers={
            "Authorization": f"Bearer {secret}",
            "apikey": secret,
            "Content-Type": content_type or "application/octet-stream",
            "x-upsert": "true",
        },
        content=buffer,
        timeout=30.0,
    )

    if response.status_code not in {200, 201}:
        detail = response.text.strip() or response.reason_phrase
        raise RuntimeError(f"Supabase storage upload failed ({response.status_code}): {detail}")

    public_url = f"{base_url}/storage/v1/object/public/{BUCKET}/{object_path}"
    verify = httpx.head(public_url, timeout=15.0)
    if verify.status_code >= 400:
        raise RuntimeError(
            "Upload saved but the campaign-assets bucket is not publicly readable. "
            "Run backend/sql/005_campaign_assets_storage.sql in Supabase, then retry."
        )

    return public_url


def ensure_campaign_assets_bucket() -> None:
    """Create or update the public campaign-assets bucket (idempotent)."""
    base_url = _supabase_url()
    secret = _supabase_secret()
    if not base_url or not secret:
        return

    headers = {
        "Authorization": f"Bearer {secret}",
        "apikey": secret,
        "Content-Type": "application/json",
    }
    payload = {"id": BUCKET, "name": BUCKET, "public": True}

    try:
        create = httpx.post(
            f"{base_url}/storage/v1/bucket",
            headers=headers,
            json=payload,
            timeout=15.0,
        )
        if create.status_code not in {200, 201, 400, 409}:
            return
        httpx.put(
            f"{base_url}/storage/v1/bucket/{BUCKET}",
            headers=headers,
            json={"public": True},
            timeout=15.0,
        )
    except httpx.HTTPError:
        return
