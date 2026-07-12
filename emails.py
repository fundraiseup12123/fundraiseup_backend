from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from db import rest_get, rest_get_one, rest_insert, rest_patch
from email_templates import (
    donation_alert_email,
    donation_confirmation_email,
    default_editable_template,
    popup_reminder_subscribed_email,
    render_editable_email,
    weekly_digest_email,
    weekly_reminder_email,
)
from email_branding import (
    DEFAULT_BRAND_NAME,
    DEFAULT_EMAIL_BANNER_URL,
    DEFAULT_EMAIL_LOGO_URL,
    DEFAULT_PRIMARY_COLOR,
)
from email_queue import RateLimited, get_email_queue
from frontend_url import resolve_frontend_url
from currency import convert_to_reporting
from db import supabase_url

from site_constants import ROOT_CAMPAIGN_ID

logger = logging.getLogger(__name__)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def resend_api_key() -> str:
    return os.getenv("RESEND_API_KEY", "").strip()


def resend_from_email() -> str:
    return os.getenv("RESEND_FROM_EMAIL", "FundraiseUp <donations@fundraiseup.com>").strip()


def resend_configured() -> bool:
    return bool(resend_api_key())


def _contact_email() -> str | None:
    raw = resend_from_email()
    if "<" in raw and ">" in raw:
        return raw.split("<", 1)[1].split(">", 1)[0].strip() or None
    return raw if "@" in raw else None


def _absolute_asset_url(url: str | None) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://") or value.startswith("data:"):
        return value
    base = resolve_frontend_url().rstrip("/")
    return f"{base}{value if value.startswith('/') else '/' + value}"


_EMAIL_SAFE_EXTS = {".png", ".jpg", ".jpeg", ".jfif", ".gif"}
_EMAIL_UNSAFE_EXTS = {".avif", ".webp", ".svg", ".svgz", ".heic", ".bmp"}


def _email_safe_image_url(url: str | None, *, fallback: str | None = None) -> str:
    """Return an absolute HTTPS image URL safe for major email clients.

    AVIF/WebP/SVG often fail in Gmail/Outlook; fall back to the platform PNG.
    """
    absolute = _absolute_asset_url(url)
    if not absolute:
        return fallback or DEFAULT_EMAIL_LOGO_URL
    if absolute.startswith("data:"):
        return fallback or DEFAULT_EMAIL_LOGO_URL

    lower = absolute.lower().split("?", 1)[0]
    for bad in _EMAIL_UNSAFE_EXTS:
        if lower.endswith(bad):
            return fallback or DEFAULT_EMAIL_LOGO_URL

    # Prefer known-safe extensions; allow extensionless CDN URLs.
    has_ext = "." in lower.rsplit("/", 1)[-1]
    if has_ext and not any(lower.endswith(ext) for ext in _EMAIL_SAFE_EXTS):
        return fallback or DEFAULT_EMAIL_LOGO_URL

    return absolute


def _brand_logo_url() -> str:
    explicit = os.getenv("EMAIL_LOGO_URL", "").strip()
    if explicit:
        return _email_safe_image_url(explicit, fallback=DEFAULT_EMAIL_LOGO_URL)
    return DEFAULT_EMAIL_LOGO_URL


def _brand_banner_url() -> str:
    explicit = os.getenv("EMAIL_BANNER_URL", "").strip()
    if explicit:
        return _email_safe_image_url(explicit, fallback=DEFAULT_EMAIL_BANNER_URL)
    return DEFAULT_EMAIL_BANNER_URL


def _unsubscribe_url(*, email: str, campaign_id: str | None = None) -> str:
    base = resolve_frontend_url().rstrip("/")
    from urllib.parse import quote

    q = f"email={quote(email)}"
    if campaign_id:
        q += f"&campaign_id={quote(str(campaign_id))}"
    # Public Next.js proxy → FastAPI
    return f"{base}/api/backend/emails/reminders/unsubscribe?{q}"


def _campaign_branding(campaign_id: str | None) -> dict[str, str]:
    defaults = {
        "title": "our campaign",
        "primary_color": DEFAULT_PRIMARY_COLOR,
        "donate_url": resolve_frontend_url(),
        "logo_url": _brand_logo_url(),
        "banner_url": _brand_banner_url(),
        "organization_name": DEFAULT_BRAND_NAME,
    }
    if not campaign_id:
        return defaults

    content = rest_get_one(
        "campaign_content",
        params={"campaign_id": f"eq.{campaign_id}", "select": "title,primary_color,logo_url,hero_url"},
    )
    campaign = rest_get_one(
        "campaigns",
        params={"id": f"eq.{campaign_id}", "select": "slug,organization_id,name"},
    )
    if content and content.get("title"):
        defaults["title"] = str(content["title"])
    elif campaign and campaign.get("name"):
        defaults["title"] = str(campaign["name"])
    if content and content.get("primary_color"):
        defaults["primary_color"] = str(content["primary_color"])
    if content and content.get("logo_url"):
        logo = _email_safe_image_url(str(content["logo_url"]), fallback="")
        if logo:
            defaults["logo_url"] = logo
    # Keep the platform watercolor banner — campaign heroes are often AVIF/JFIF
    # with content-types email clients reject.
    defaults["banner_url"] = _brand_banner_url()
    if campaign and campaign.get("slug"):
        base = resolve_frontend_url().rstrip("/")
        defaults["donate_url"] = f"{base}/c/{campaign['slug']}"
    if campaign and campaign.get("organization_id"):
        org_id = str(campaign["organization_id"])
        defaults["organization_id"] = org_id
        org = rest_get_one(
            "organizations",
            params={"id": f"eq.{org_id}", "select": "name"},
        )
        if org and org.get("name"):
            defaults["organization_name"] = str(org["name"])
    return defaults


def _supabase_admin_headers() -> dict[str, str]:
    secret = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    return {
        "apikey": secret,
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
    }


def get_user_email_by_id(user_id: str) -> str | None:
    url = supabase_url()
    secret = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    if not url or not secret:
        return None
    try:
        response = httpx.get(
            f"{url}/auth/v1/admin/users/{user_id}",
            headers=_supabase_admin_headers(),
            timeout=20.0,
        )
        if response.status_code == 200:
            email = response.json().get("email")
            return str(email).strip().lower() if email else None
    except httpx.HTTPError as exc:
        logger.warning("Supabase user lookup failed for %s: %s", user_id, exc)
    return None


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


def get_org_email_template(organization_id: str | None, template_key: str) -> dict[str, Any] | None:
    if not organization_id:
        return None
    try:
        return rest_get_one(
            "email_templates",
            params={
                "organization_id": f"eq.{organization_id}",
                "template_key": f"eq.{template_key}",
                "select": "template_key,subject,headline,body_html,banner_url,logo_url,cta_label",
            },
        )
    except Exception:
        return None


def _fmt_amount_token(amount: float | str, currency: str) -> str:
    try:
        value = float(amount)
        return f"{value:,.2f} {str(currency).upper()}"
    except (TypeError, ValueError):
        return f"{amount} {currency}".strip()


def compose_templated_email(
    *,
    template_key: str,
    organization_id: str | None,
    tokens: dict[str, str],
    logo_url: str,
    primary_color: str,
    organization_name: str | None,
    banner_url: str | None,
    cta_url: str | None,
    contact_email: str | None,
    unsubscribe_url: str | None = None,
    fallback: tuple[str, str] | None = None,
) -> tuple[str, str]:
    """Use org override when present; otherwise return provided fallback builders."""
    override = get_org_email_template(organization_id, template_key)
    if override:
        return render_editable_email(
            template=override,
            tokens=tokens,
            logo_url=logo_url,
            primary_color=primary_color,
            organization_name=organization_name,
            banner_url=banner_url,
            cta_url=cta_url,
            contact_email=contact_email,
            unsubscribe_url=unsubscribe_url,
        )
    if fallback:
        return fallback
    defaults = default_editable_template(template_key)
    return render_editable_email(
        template=defaults,
        tokens=tokens,
        logo_url=logo_url,
        primary_color=primary_color,
        organization_name=organization_name,
        banner_url=banner_url,
        cta_url=cta_url,
        contact_email=contact_email,
        unsubscribe_url=unsubscribe_url,
    )


def _parse_retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("retry-after") or response.headers.get("ratelimit-reset") or "1"
    try:
        return max(0.25, float(raw))
    except ValueError:
        return 1.0


def _parse_rate_limit(response: httpx.Response) -> float | None:
    raw = response.headers.get("ratelimit-limit")
    if not raw:
        return None
    try:
        # Headers may be "2" or "2;w=1"
        return max(0.1, float(str(raw).split(";", 1)[0].strip()))
    except ValueError:
        return None


def _inline_remote_images(html: str) -> tuple[str, list[dict[str, str]]]:
    """Rewrite remote <img src="https://..."> to cid: and collect Resend attachments.

    Inline CIDs render even when Gmail blocks remote images (common in Spam).
    """
    import re

    attachments: list[dict[str, str]] = []
    seen: dict[str, str] = {}

    def repl(match: re.Match[str]) -> str:
        url = match.group(1)
        if url.startswith("cid:"):
            return match.group(0)
        if url in seen:
            return f'src="cid:{seen[url]}"'
        cid = f"fu-img-{len(attachments) + 1}"
        filename = url.rsplit("/", 1)[-1].split("?", 1)[0] or "image.png"
        if "." not in filename:
            filename = f"{filename}.png"
        attachments.append(
            {
                "path": url,
                "filename": filename,
                "content_id": cid,
            }
        )
        seen[url] = cid
        return f'src="cid:{cid}"'

    rewritten = re.sub(r'src="(https?://[^"]+)"', repl, html)
    return rewritten, attachments


def _deliver_resend_email(
    *,
    to: str,
    subject: str,
    html: str,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """POST one email to Resend. Raises RateLimited on HTTP 429."""
    payload: dict[str, Any] = {
        "from": resend_from_email(),
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if attachments:
        payload["attachments"] = attachments

    response = httpx.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {resend_api_key()}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=45.0,
    )

    limit = _parse_rate_limit(response)
    if limit is not None:
        get_email_queue(_deliver_resend_email).set_max_per_second(limit)

    if response.status_code == 429:
        raise RateLimited(retry_after=_parse_retry_after(response), limit=limit)

    if response.status_code >= 400:
        detail = response.text
        try:
            detail = response.json().get("message", detail)
        except Exception:
            pass
        raise RuntimeError(detail or "Failed to send email")

    body = response.json()
    return {"sent": True, "id": body.get("id")}


def send_resend_email(*, to: str, subject: str, html: str) -> dict[str, Any]:
    """Enqueue email send (rate-limited) and wait for delivery."""
    if not resend_configured():
        logger.warning("RESEND_API_KEY not set — skipping email to %s", to)
        return {"sent": False, "reason": "not_configured"}

    # Do not CID-inline images: Gmail webmail often shows them as file attachments
    # instead of rendering them in the body (especially in Spam).
    return get_email_queue(_deliver_resend_email).submit(to=to, subject=subject, html=html)


def _email_presentation(
    branding: dict[str, str],
    *,
    recipient_email: str | None = None,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    cid = campaign_id or None
    return {
        "logo_url": branding.get("logo_url", _brand_logo_url()),
        "banner_url": branding.get("banner_url", _brand_banner_url()),
        "organization_name": branding.get("organization_name", DEFAULT_BRAND_NAME),
        "campaign_title": branding.get("title", "our campaign"),
        "contact_email": _contact_email(),
        "unsubscribe_url": (
            _unsubscribe_url(email=recipient_email, campaign_id=cid)
            if recipient_email
            else None
        ),
    }


def send_donation_confirmation_for_row(row: dict[str, Any]) -> bool:
    email = (row.get("email") or "").strip()
    if not email or "@" not in email:
        return False

    campaign_id = row.get("campaign_id")
    branding = _campaign_branding(str(campaign_id) if campaign_id else ROOT_CAMPAIGN_ID)
    donor_name = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip() or "Friend"
    extras = _email_presentation(
        branding,
        recipient_email=email,
        campaign_id=str(campaign_id) if campaign_id else None,
    )

    subject, html = compose_templated_email(
        template_key="donation_confirmation",
        organization_id=branding.get("organization_id") or row.get("organization_id"),
        tokens={
            "donor_name": donor_name,
            "amount": _fmt_amount_token(row.get("amount", 0), str(row.get("currency", "USD"))),
            "campaign_title": str(branding.get("title", "our campaign")),
            "org_name": str(extras["organization_name"]),
            "admin_name": "",
            "donation_count": "",
            "total_raised": "",
        },
        logo_url=extras["logo_url"],
        primary_color=str(branding.get("primary_color", DEFAULT_PRIMARY_COLOR)),
        organization_name=str(branding.get("title") or extras["organization_name"]),
        banner_url=extras["banner_url"],
        cta_url=str(branding.get("donate_url", resolve_frontend_url())),
        contact_email=extras["contact_email"],
        fallback=donation_confirmation_email(
            donor_name=donor_name,
            amount=row.get("amount", 0),
            currency=str(row.get("currency", "USD")),
            frequency=str(row.get("frequency", "once")),
            campaign_title=str(branding.get("title", "our campaign")),
            logo_url=extras["logo_url"],
            donate_url=str(branding.get("donate_url", resolve_frontend_url())),
            primary_color=str(branding.get("primary_color", DEFAULT_PRIMARY_COLOR)),
            organization_name=str(branding.get("title") or extras["organization_name"]),
            banner_url=extras["banner_url"],
            contact_email=extras["contact_email"],
        ),
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
    extras = _email_presentation(
        branding,
        recipient_email=normalized,
        campaign_id=str(campaign),
    )

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

    subject, html = compose_templated_email(
        template_key="reminder_subscribed",
        organization_id=branding.get("organization_id"),
        tokens={
            "donor_name": donor_name or "",
            "amount": "",
            "campaign_title": str(branding.get("title", "our campaign")),
            "org_name": extras["organization_name"],
            "admin_name": "",
            "donation_count": "",
            "total_raised": "",
        },
        logo_url=extras["logo_url"],
        primary_color=str(branding.get("primary_color", DEFAULT_PRIMARY_COLOR)),
        organization_name=extras["organization_name"],
        banner_url=extras["banner_url"],
        cta_url=str(branding.get("donate_url", resolve_frontend_url())),
        contact_email=extras["contact_email"],
        unsubscribe_url=extras["unsubscribe_url"],
        fallback=popup_reminder_subscribed_email(
            campaign_title=str(branding.get("title", "our campaign")),
            logo_url=extras["logo_url"],
            donate_url=str(branding.get("donate_url", resolve_frontend_url())),
            primary_color=str(branding.get("primary_color", DEFAULT_PRIMARY_COLOR)),
            organization_name=extras["organization_name"],
            banner_url=extras["banner_url"],
            contact_email=extras["contact_email"],
            unsubscribe_url=extras["unsubscribe_url"],
        ),
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


def unsubscribe_weekly_reminder(
    *,
    email: str,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    normalized = email.strip().lower()
    if not normalized or "@" not in normalized:
        return {"unsubscribed": False, "reason": "invalid_email"}

    params: dict[str, str] = {
        "email": f"eq.{normalized}",
        "select": "id",
    }
    if campaign_id:
        params["campaign_id"] = f"eq.{campaign_id}"

    rows = rest_get("email_reminders", params=params)
    if not rows:
        return {"unsubscribed": True, "updated": 0}

    updated = 0
    for row in rows:
        rest_patch("email_reminders", {"active": False}, match={"id": row["id"]})
        updated += 1
    return {"unsubscribed": True, "updated": updated}


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
        extras = _email_presentation(
            branding,
            recipient_email=str(email),
            campaign_id=str(campaign_id),
        )

        subject, html = compose_templated_email(
            template_key="weekly_reminder",
            organization_id=branding.get("organization_id"),
            tokens={
                "donor_name": str(reminder.get("donor_name") or ""),
                "amount": "",
                "campaign_title": str(branding.get("title", "our campaign")),
                "org_name": extras["organization_name"],
                "admin_name": "",
                "donation_count": "",
                "total_raised": "",
            },
            logo_url=extras["logo_url"],
            primary_color=str(branding.get("primary_color", DEFAULT_PRIMARY_COLOR)),
            organization_name=extras["organization_name"],
            banner_url=extras["banner_url"],
            cta_url=str(branding.get("donate_url", resolve_frontend_url())),
            contact_email=extras["contact_email"],
            unsubscribe_url=extras["unsubscribe_url"],
            fallback=weekly_reminder_email(
                donor_name=reminder.get("donor_name"),
                campaign_title=str(branding.get("title", "our campaign")),
                logo_url=extras["logo_url"],
                donate_url=str(branding.get("donate_url", resolve_frontend_url())),
                primary_color=str(branding.get("primary_color", DEFAULT_PRIMARY_COLOR)),
                organization_name=extras["organization_name"],
                banner_url=extras["banner_url"],
                contact_email=extras["contact_email"],
                unsubscribe_url=extras["unsubscribe_url"],
            ),
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
        extras = _email_presentation(
            branding,
            recipient_email=email,
            campaign_id=str(row.get("campaign_id") or "") or None,
        )
        donor_name = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip() or None

        subject, html = compose_templated_email(
            template_key="weekly_donor_reminder",
            organization_id=row.get("organization_id") or branding.get("organization_id"),
            tokens={
                "donor_name": donor_name or "",
                "amount": "",
                "campaign_title": str(branding.get("title", "our campaign")),
                "org_name": extras["organization_name"],
                "admin_name": "",
                "donation_count": "",
                "total_raised": "",
            },
            logo_url=extras["logo_url"],
            primary_color=str(branding.get("primary_color", DEFAULT_PRIMARY_COLOR)),
            organization_name=extras["organization_name"],
            banner_url=extras["banner_url"],
            cta_url=str(branding.get("donate_url", resolve_frontend_url())),
            contact_email=extras["contact_email"],
            unsubscribe_url=extras["unsubscribe_url"],
            fallback=weekly_reminder_email(
                donor_name=donor_name,
                campaign_title=str(branding.get("title", "our campaign")),
                logo_url=extras["logo_url"],
                donate_url=str(branding.get("donate_url", resolve_frontend_url())),
                primary_color=str(branding.get("primary_color", DEFAULT_PRIMARY_COLOR)),
                organization_name=extras["organization_name"],
                banner_url=extras["banner_url"],
                contact_email=extras["contact_email"],
                unsubscribe_url=extras["unsubscribe_url"],
            ),
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


def _org_notification_prefs(org_id: str) -> dict[str, Any]:
    org = rest_get_one(
        "organizations",
        params={"id": f"eq.{org_id}", "select": "notification_prefs"},
    )
    prefs = (org or {}).get("notification_prefs") or {}
    return prefs if isinstance(prefs, dict) else {}


def _org_notification_recipients(org_id: str) -> list[dict[str, str]]:
    members = rest_get(
        "organization_members",
        params={
            "organization_id": f"eq.{org_id}",
            "select": "user_id,profiles(first_name,last_name)",
        },
    )
    recipients: list[dict[str, str]] = []
    seen: set[str] = set()
    for member in members:
        user_id = str(member.get("user_id") or "")
        if not user_id or user_id in seen:
            continue
        email = get_user_email_by_id(user_id)
        if not email:
            continue
        seen.add(user_id)
        profile = member.get("profiles") or {}
        if isinstance(profile, list):
            profile = profile[0] if profile else {}
        name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
        recipients.append({"email": email, "name": name or email})
    return recipients


def send_donation_alerts_for_row(row: dict[str, Any]) -> int:
    if not resend_configured():
        return 0

    org_id = row.get("organization_id")
    if not org_id:
        return 0

    prefs = _org_notification_prefs(str(org_id))
    if prefs.get("donation_alerts") is False:
        return 0

    org = rest_get_one(
        "organizations",
        params={"id": f"eq.{org_id}", "select": "name"},
    )
    organization_name = str((org or {}).get("name") or "your organization")
    campaign_title = "your campaign"
    campaign_id = row.get("campaign_id")
    if campaign_id:
        content = rest_get_one(
            "campaign_content",
            params={"campaign_id": f"eq.{campaign_id}", "select": "title"},
        )
        if content and content.get("title"):
            campaign_title = str(content["title"])

    donor_name = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip() or "A supporter"
    admin_url = f"{resolve_frontend_url().rstrip('/')}/admin/donations"
    if row.get("id"):
        admin_url = f"{admin_url}/{row['id']}"
    logo_url = _brand_logo_url()
    banner_url = _brand_banner_url()
    contact = _contact_email()
    sent = 0

    for recipient in _org_notification_recipients(str(org_id)):
        admin_greeting = f", {recipient['name']}" if recipient.get("name") else ""
        subject, html = compose_templated_email(
            template_key="donation_alert",
            organization_id=str(org_id),
            tokens={
                "donor_name": donor_name,
                "amount": _fmt_amount_token(row.get("amount", 0), str(row.get("currency", "USD"))),
                "campaign_title": campaign_title,
                "org_name": organization_name,
                "admin_name": admin_greeting,
                "donation_count": "",
                "total_raised": "",
            },
            logo_url=logo_url,
            primary_color=DEFAULT_PRIMARY_COLOR,
            organization_name=organization_name,
            banner_url=banner_url,
            cta_url=admin_url,
            contact_email=contact,
            fallback=donation_alert_email(
                admin_name=recipient["name"],
                donor_name=donor_name,
                amount=row.get("amount", 0),
                currency=str(row.get("currency", "USD")),
                campaign_title=campaign_title,
                organization_name=organization_name,
                admin_url=admin_url,
                logo_url=logo_url,
                primary_color=DEFAULT_PRIMARY_COLOR,
                banner_url=banner_url,
                contact_email=contact,
            ),
        )
        try:
            send_resend_email(to=recipient["email"], subject=subject, html=html)
            log_email(
                recipient_email=recipient["email"],
                subject=subject,
                template_key="donation_alert",
                organization_id=str(org_id),
                donation_id=str(row["id"]) if row.get("id") else None,
            )
            sent += 1
        except RuntimeError as exc:
            logger.error("Donation alert failed for %s: %s", recipient["email"], exc)

    return sent


def send_org_weekly_digests() -> dict[str, int]:
    if not resend_configured():
        return {"digests_sent": 0, "skipped": 0}

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    orgs = rest_get(
        "organizations",
        params={"status": "eq.active", "select": "id,name,notification_prefs,reporting_currency"},
    )
    digests_sent = 0
    skipped = 0

    for org in orgs:
        org_id = str(org.get("id") or "")
        if not org_id:
            continue
        prefs = org.get("notification_prefs") or {}
        if not isinstance(prefs, dict) or not prefs.get("weekly_digest"):
            continue

        recent_digest = rest_get(
            "email_logs",
            params={
                "organization_id": f"eq.{org_id}",
                "template_key": "eq.org_weekly_digest",
                "sent_at": f"gte.{week_ago.isoformat()}",
                "select": "id",
                "limit": "1",
            },
        )
        if recent_digest:
            skipped += 1
            continue

        donations = rest_get(
            "donations",
            params={
                "organization_id": f"eq.{org_id}",
                "status": "eq.succeeded",
                "created_at": f"gte.{week_ago.isoformat()}",
                "select": "amount,currency",
            },
        )
        reporting_currency = str(org.get("reporting_currency") or "USD").upper()
        total_raised = sum(
            convert_to_reporting(float(row.get("amount") or 0), str(row.get("currency") or "USD"), reporting_currency)
            for row in donations
        )
        admin_url = f"{resolve_frontend_url().rstrip('/')}/admin/insights"
        logo_url = _brand_logo_url()
        banner_url = _brand_banner_url()
        contact = _contact_email()
        organization_name = str(org.get("name") or "your organization")

        for recipient in _org_notification_recipients(org_id):
            admin_greeting = f", {recipient['name']}" if recipient.get("name") else ""
            amount_label = _fmt_amount_token(round(total_raised, 2), reporting_currency)
            subject, html = compose_templated_email(
                template_key="org_weekly_digest",
                organization_id=org_id,
                tokens={
                    "donor_name": "",
                    "amount": amount_label,
                    "campaign_title": organization_name,
                    "org_name": organization_name,
                    "admin_name": admin_greeting,
                    "donation_count": str(len(donations)),
                    "total_raised": amount_label,
                },
                logo_url=logo_url,
                primary_color=DEFAULT_PRIMARY_COLOR,
                organization_name=organization_name,
                banner_url=banner_url,
                cta_url=admin_url,
                contact_email=contact,
                fallback=weekly_digest_email(
                    admin_name=recipient["name"],
                    organization_name=organization_name,
                    donation_count=len(donations),
                    total_raised=round(total_raised, 2),
                    reporting_currency=reporting_currency,
                    admin_url=admin_url,
                    logo_url=logo_url,
                    primary_color=DEFAULT_PRIMARY_COLOR,
                    banner_url=banner_url,
                    contact_email=contact,
                ),
            )
            try:
                send_resend_email(to=recipient["email"], subject=subject, html=html)
                log_email(
                    recipient_email=recipient["email"],
                    subject=subject,
                    template_key="org_weekly_digest",
                    organization_id=org_id,
                )
                digests_sent += 1
            except RuntimeError as exc:
                logger.error("Weekly digest failed for %s: %s", recipient["email"], exc)
                skipped += 1

    return {"digests_sent": digests_sent, "skipped": skipped}
