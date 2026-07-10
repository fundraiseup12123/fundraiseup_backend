from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from auth import AuthUser, require_auth
from storage_upload import supabase_storage_configured, upload_campaign_asset

router = APIRouter(tags=["uploads"])

MAX_BYTES = 5 * 1024 * 1024
ALLOWED_TYPES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/avif",
    "image/x-icon",
    "image/vnd.microsoft.icon",
    "image/svg+xml",
}


@router.post("/admin/upload")
async def admin_upload_asset(
    user: Annotated[AuthUser, Depends(require_auth)],
    file: UploadFile = File(...),
) -> dict[str, str]:
    if not supabase_storage_configured():
        raise HTTPException(
            status_code=503,
            detail="Supabase storage is not configured. Set SUPABASE_URL and SUPABASE_SECRET_KEY on the backend.",
        )

    content = await file.read()
    if len(content) > MAX_BYTES:
        raise HTTPException(status_code=400, detail="File must be 5 MB or smaller")

    content_type = (file.content_type or "application/octet-stream").lower()
    if content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    try:
        url = upload_campaign_asset(content, file.filename or "upload.png", content_type)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"url": url}
