from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from db import rest_get, rest_get_one, rest_insert, rest_patch
from email_templates import (
    donation_confirmation_email,
    popup_reminder_subscribed_email,
    weekly_reminder_email,
)
from email_branding import DEFAULT_BRAND_NAME, DEFAULT_EMAIL_LOGO_URL, DEFAULT_PRIMARY_COLOR
from frontend_url import resolve_frontend_url
from site_constants import ROOT_CAMPAIGN_ID

logger = logging.getLogger(__name__)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def resend_api_key() -> str:
    return os.getenv("RESEND_API_KEY", "").strip()


def resend_from_email() -> str:
    return os.getenv("RESEND_FROM_EMAIL", "Fundraise <donations@fundraiseup.com>").strip()


def resend_configured() -> bool:
    return bool(resend_api_key())


def _brand_logo_url() -> str:
    explicit = os.getenv("EMAIL_LOGO_URL", "").strip()
    if explicit:
        return explicit
    return DEFAULT_EMAIL_LOGO_URL


def _campaign_branding(campaign_id: str | None) -> dict[str, str]:
    defaults = {
        "title": "our campaign",
        "primary_color": "#3872DC",
        "donate_url": resolve_frontend_url(),
    }
    if not campaign_id:
        return defaults

    content = rest_get_one(
        "campaign_content",
        params={"campaign_id": f"eq.{campaign_id}", "select": "title,primary_color,logo_url"},
    )
    campaign = rest_get_one(
        "campaigns",
        params={"id": f"eq.{campaign_id}", "select": "slug,organization_id"},
    )
    if content and content.get("title"):
        defaults["title"] = str(content["title"])
    if content and content.get("primary_color"):
        defaults["primary_color"] = str(content["primary_color"])
    if content and content.get("logo_url"):
        logo = str(content["logo_url"])
        if logo.startswith("http"):
            defaults["logo_url"] = logo
    if campaign and campaign.get("slug"):
        base = resolve_frontend_url().rstrip("/")
        defaults["donate_url"] = f"{base}/?campaign={campaign['slug']}"
    if campaign and campaign.get("organization_id"):
        defaults["organization_id"] = str(campaign["organization_id"])
    return defaults


def log_email(
    *,
    recipient_email: str,
    subject: str,
    template_key: str,
    organization_id: str | None = None,
    donation_id: str | None = None,
) -> dict[str, Any] | None:
    return rest_insert(
        "email_logs",
        {
            "recipient_email": recipient_email,
            "subject": subject,
            "template_key": template_key,
            "organization_id": organization_id,
            "donation_id": donation_id,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def send_resend_email(*, to: str, subject: str, html: str) -> dict[str, Any]:
    if not resend_configured():
        logger.warning("RESEND_API_KEY not set — skipping email to %s", to)
        return {"sent": False, "reason": "not_configured"}

    response = httpx.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {resend_api_key()}",
            "Content-Type": "application/json",
        },
        json={
            "from": resend_from_email(),
            "to": [to],
            "subject": subject,
            "html": html,
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        detail = response.text
        try:
            detail = response.json().get("message", detail)
        except Exception:
            pass
        raise RuntimeError(detail or "Failed to send email")

    body = response.json()
    return {"sent": True, "id": body.get("id")}


def send_donation_confirmation_for_row(row: dict[str, Any]) -> bool:
    email = (row.get("email") or "").strip()
    if not email or "@" not in email:
        return False

    campaign_id = row.get("campaign_id")
    branding = _campaign_branding(str(campaign_id) if campaign_id else ROOT_CAMPAIGN_ID)
    donor_name = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip() or "Friend"
    logo_url = branding.get("logo_url", _brand_logo_url())

    subject, html = donation_confirmation_email(
        donor_name=donor_name,
        amount=row.get("amount", 0),
        currency=str(row.get("currency", "USD")),
        frequency=str(row.get("frequency", "once")),
        campaign_title=str(branding.get("title", "our campaign")),
        logo_url=logo_url,
        donate_url=str(branding.get("donate_url", resolve_frontend_url())),
        primary_color=str(branding.get("primary_color", "#3872DC")),
    )

    try:
        send_resend_email(to=email, subject=subject, html=html)
        log_email(
            recipient_email=email,
            subject=subject,
            template_key="donation_confirmation",
            organization_id=branding.get("organization_id") or row.get("organization_id"),
            donation_id=str(row["id"]) if row.get("id") else None,
        )
        return True
    except RuntimeError as exc:
        logger.error("Donation confirmation email failed for %s: %s", email, exc)
        return False


def subscribe_weekly_reminder(
    *,
    email: str,
    campaign_id: str | None = None,
    source: str = "popup",
    donor_name: str | None = None,
) -> dict[str, Any]:
    normalized = email.strip().lower()
    campaign = campaign_id or ROOT_CAMPAIGN_ID
    branding = _campaign_branding(campaign)
    logo_url = branding.get("logo_url", _brand_logo_url())

    existing = rest_get_one(
        "email_reminders",
        params={
            "email": f"eq.{normalized}",
            "campaign_id": f"eq.{campaign}",
            "select": "id,active",
        },
    )
    if not existing:
        rest_insert(
            "email_reminders",
            {
                "email": normalized,
                "campaign_id": campaign,
                "organization_id": branding.get("organization_id"),
                "source": source,
                "donor_name": donor_name,
                "active": True,
            },
        )
    elif existing.get("active") is False:
        rest_patch("email_reminders", {"active": True}, match={"id": existing["id"]})

    subject, html = popup_reminder_subscribed_email(
        campaign_title=str(branding.get("title", "our campaign")),
        logo_url=logo_url,
        donate_url=str(branding.get("donate_url", resolve_frontend_url())),
        primary_color=str(branding.get("primary_color", "#3872DC")),
    )
    try:
        send_resend_email(to=normalized, subject=subject, html=html)
        log_email(
            recipient_email=normalized,
            subject=subject,
            template_key="reminder_subscribed",
            organization_id=branding.get("organization_id"),
        )
    except RuntimeError as exc:
        logger.warning("Reminder subscribe email failed: %s", exc)

    return {"subscribed": True}


def send_weekly_reminders() -> dict[str, int | str]:
    if not resend_configured():
        return {"sent": 0, "skipped": 0, "reason": "not_configured"}

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    reminders = rest_get(
        "email_reminders",
        params={
            "active": "eq.true",
            "select": "id,email,campaign_id,donor_name,last_sent_at",
            "limit": "200",
        },
    )
    reminders = [
        r
        for r in reminders
        if not r.get("last_sent_at")
        or _parse_dt(r["last_sent_at"]) <= week_ago
    ]

    sent = 0
    skipped = 0
    for reminder in reminders:
        email = reminder.get("email")
        if not email:
            skipped += 1
            continue

        campaign_id = reminder.get("campaign_id") or ROOT_CAMPAIGN_ID
        branding = _campaign_branding(str(campaign_id))
        logo_url = branding.get("logo_url", _brand_logo_url())

        subject, html = weekly_reminder_email(
            donor_name=reminder.get("donor_name"),
            campaign_title=str(branding.get("title", "our campaign")),
            logo_url=logo_url,
            donate_url=str(branding.get("donate_url", resolve_frontend_url())),
            primary_color=str(branding.get("primary_color", "#3872DC")),
        )

        try:
            send_resend_email(to=email, subject=subject, html=html)
            log_email(
                recipient_email=email,
                subject=subject,
                template_key="weekly_reminder",
                organization_id=branding.get("organization_id"),
            )
            rest_patch(
                "email_reminders",
                {"last_sent_at": datetime.now(timezone.utc).isoformat()},
                match={"id": reminder["id"]},
            )
            sent += 1
        except RuntimeError as exc:
            logger.error("Weekly reminder failed for %s: %s", email, exc)
            skipped += 1

    donor_rows = rest_get(
        "donations",
        params={
            "select": "id,email,first_name,last_name,campaign_id,organization_id",
            "email": "not.is.null",
            "status": "eq.succeeded",
            "order": "created_at.desc",
            "limit": "500",
        },
    )
    seen_emails: set[str] = {r.get("email", "").lower() for r in reminders if r.get("email")}

    for row in donor_rows:
        email = (row.get("email") or "").strip().lower()
        if not email or email in seen_emails:
            continue

        reminder = rest_get_one(
            "email_reminders",
            params={
                "email": f"eq.{email}",
                "source": "eq.donor",
                "select": "id,last_sent_at",
            },
        )
        if reminder and reminder.get("last_sent_at"):
            try:
                if datetime.now(timezone.utc) - _parse_dt(str(reminder["last_sent_at"])) < timedelta(days=7):
                    continue
            except ValueError:
                pass

        branding = _campaign_branding(row.get("campaign_id"))
        logo_url = branding.get("logo_url", _brand_logo_url())
        donor_name = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip() or None

        subject, html = weekly_reminder_email(
            donor_name=donor_name,
            campaign_title=str(branding.get("title", "our campaign")),
            logo_url=logo_url,
            donate_url=str(branding.get("donate_url", resolve_frontend_url())),
            primary_color=str(branding.get("primary_color", "#3872DC")),
        )

        try:
            send_resend_email(to=email, subject=subject, html=html)
            log_email(
                recipient_email=email,
                subject=subject,
                template_key="weekly_donor_reminder",
                organization_id=row.get("organization_id") or branding.get("organization_id"),
                donation_id=str(row["id"]) if row.get("id") else None,
            )
            if reminder:
                rest_patch(
                    "email_reminders",
                    {"last_sent_at": datetime.now(timezone.utc).isoformat()},
                    match={"id": reminder["id"]},
                )
            else:
                rest_insert(
                    "email_reminders",
                    {
                        "email": email,
                        "campaign_id": row.get("campaign_id") or ROOT_CAMPAIGN_ID,
                        "organization_id": row.get("organization_id") or branding.get("organization_id"),
                        "source": "donor",
                        "donor_name": donor_name,
                        "active": True,
                        "last_sent_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            seen_emails.add(email)
            sent += 1
        except RuntimeError as exc:
            logger.error("Weekly donor reminder failed for %s: %s", email, exc)
            skipped += 1

    return {"sent": sent, "skipped": skipped}
