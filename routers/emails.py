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
    send_resend_email,
    send_weekly_reminders,
    subscribe_weekly_reminder,
    unsubscribe_weekly_reminder,
)

router = APIRouter(prefix="/emails", tags=["emails"])


class ReminderSubscribeRequest(BaseModel):
    email: EmailStr
    campaign_id: str | None = None
    donor_name: str | None = Field(default=None, max_length=160)


class ContactFormRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=40)
    subject: str | None = Field(default=None, max_length=160)
    message: str = Field(min_length=1, max_length=4000)
    # Honeypot — bots fill this; humans never see it.
    company: str | None = Field(default=None, max_length=200)


def _escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


@router.get("/status")
def email_status() -> dict[str, bool]:
    return {"configured": resend_configured()}


@router.post("/contact")
def contact_form(payload: ContactFormRequest) -> dict[str, Any]:
    """Public contact form (Hope for Gaza landing, etc.). Uses platform Resend keys."""
    if (payload.company or "").strip():
        return {"ok": True}

    if not resend_configured():
        raise HTTPException(
            status_code=503,
            detail="Email is not configured. Please try again later or email us directly.",
        )

    name = payload.name.strip()
    email = str(payload.email).strip()
    phone = (payload.phone or "").strip()
    subject = (payload.subject or "").strip() or "Website contact form"
    message = payload.message.strip()
    if not name or not message:
        raise HTTPException(status_code=400, detail="Name, email, and message are required.")

    to_address = (
        os.getenv("CONTACT_TO_EMAIL", "").strip()
        or "info@hopeforgaza.foundation"
    )

    html = f"""
    <h2>New contact form message</h2>
    <p><strong>Name:</strong> {_escape_html(name)}</p>
    <p><strong>Email:</strong> {_escape_html(email)}</p>
    {f"<p><strong>Phone:</strong> {_escape_html(phone)}</p>" if phone else ""}
    <p><strong>Subject:</strong> {_escape_html(subject)}</p>
    <hr />
    <p style="white-space:pre-wrap">{_escape_html(message)}</p>
    """

    try:
        result = send_resend_email(
            to=to_address,
            subject=f"[Contact] {subject}",
            html=html,
            reply_to=email,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Failed to send message. Please try again.") from exc

    if not result.get("sent"):
        raise HTTPException(
            status_code=503,
            detail="Email is not configured. Please try again later or email us directly.",
        )
    return {"ok": True}


@router.post("/reminders/subscribe")
def subscribe_reminder(payload: ReminderSubscribeRequest) -> dict[str, Any]:
    return subscribe_weekly_reminder(
        email=str(payload.email),
        campaign_id=payload.campaign_id,
        source="popup",
        donor_name=payload.donor_name,
    )


@router.get("/reminders/unsubscribe")
def unsubscribe_reminder(
    email: EmailStr,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    return unsubscribe_weekly_reminder(email=str(email), campaign_id=campaign_id)


@router.post("/reminders/unsubscribe")
def unsubscribe_reminder_one_click(
    email: EmailStr | None = None,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    """Gmail/Yahoo one-click unsubscribe (List-Unsubscribe-Post)."""
    if email is None:
        return {"unsubscribed": False, "reason": "missing_email"}
    return unsubscribe_weekly_reminder(email=str(email), campaign_id=campaign_id)


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
