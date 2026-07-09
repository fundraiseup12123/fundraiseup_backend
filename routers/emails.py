from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field

from auth import AuthUser, require_auth, require_org_access
from db import rest_get
from emails import (
    resend_configured,
    send_org_weekly_digests,
    send_weekly_reminders,
    subscribe_weekly_reminder,
)

router = APIRouter(prefix="/emails", tags=["emails"])


class ReminderSubscribeRequest(BaseModel):
    email: EmailStr
    campaign_id: str | None = None
    donor_name: str | None = Field(default=None, max_length=160)


@router.get("/status")
def email_status() -> dict[str, bool]:
    return {"configured": resend_configured()}


@router.post("/reminders/subscribe")
def subscribe_reminder(payload: ReminderSubscribeRequest) -> dict[str, Any]:
    return subscribe_weekly_reminder(
        email=str(payload.email),
        campaign_id=payload.campaign_id,
        source="popup",
        donor_name=payload.donor_name,
    )


@router.post("/cron/weekly-reminders")
def cron_weekly_reminders(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    secret = os.getenv("CRON_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="CRON_SECRET not configured")
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token != secret:
        raise HTTPException(status_code=401, detail="Unauthorized")
    reminders = send_weekly_reminders()
    digests = send_org_weekly_digests()
    return {**reminders, **digests}


@router.get("/orgs/{org_id}/logs")
def list_org_email_logs(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    limit: int = 100,
) -> list[dict[str, Any]]:
    require_org_access(org_id, user, min_role="member")
    rows = rest_get(
        "email_logs",
        params={
            "organization_id": f"eq.{org_id}",
            "select": "id,recipient_email,subject,template_key,sent_at,opened_at,donation_id",
            "order": "sent_at.desc",
            "limit": str(min(limit, 200)),
        },
    )
    if rows:
        return rows

    donations = rest_get(
        "donations",
        params={"organization_id": f"eq.{org_id}", "select": "id", "limit": "500"},
    )
    donation_ids = [d["id"] for d in donations]
    if not donation_ids:
        return []
    ids_filter = ",".join(donation_ids)
    return rest_get(
        "email_logs",
        params={
            "donation_id": f"in.({ids_filter})",
            "select": "id,recipient_email,subject,template_key,sent_at,opened_at,donation_id",
            "order": "sent_at.desc",
            "limit": str(min(limit, 200)),
        },
    )
