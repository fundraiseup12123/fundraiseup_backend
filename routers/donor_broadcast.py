from __future__ import annotations

import logging
import re
from typing import Annotated, Any, Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import AuthUser, require_auth, require_super_admin
from db import rest_get, rest_get_one
from emails import resend_configured, send_resend_email

logger = logging.getLogger(__name__)

router = APIRouter(tags=["donor_broadcast"])

HOPE_FOR_GAZA_TEMPLATE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="icon" type="image/png" href="https://fundraiseup.us.com/favicon.png">
  <link rel="shortcut icon" href="https://fundraiseup.us.com/favicon.png">
  <title>Thank You for Helping Families in Gaza</title>
</head>
<body style="margin:0;padding:0;background:#f5f7f3;color:#202124;font-family:Arial,Helvetica,sans-serif;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">Your donation helped provide hot meals, shelter, and baby milk in Gaza. More families still need help.</div>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f5f7f3;">
    <tr><td align="center" style="padding:25px 12px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:650px;background:#ffffff;border-radius:10px;overflow:hidden;">
        <tr><td align="center" style="padding:22px 24px 16px;border-top:7px solid #ad2436;">
          <div style="margin-bottom:10px;">
            <img src="https://fundraiseup.us.com/favicon.png" width="36" height="36" alt="FundraiseUp" style="display:inline-block;width:36px;height:36px;border-radius:50%;vertical-align:middle;box-shadow:0 2px 6px rgba(0,0,0,0.15);border:2px solid #ffffff;">
          </div>
          <img src="https://files.manuscdn.com/user_upload_by_module/session_file/310519663541564405/wTpeSppOdlRBzhKE.jpg" width="245" alt="Hope for Gaza Foundation" style="display:block;width:245px;max-width:80%;height:auto;border:0;margin:0 auto;">
        </td></tr>
        <tr><td style="padding:10px 42px 36px;">
          <h1 style="margin:0 0 20px;color:#ad2436;font-size:27px;line-height:1.25;">Your kindness is making a difference in Gaza</h1>
          <p style="margin:0 0 17px;font-size:16px;line-height:1.7;">Dear <strong>{{DONOR_NAME}}</strong>,</p>
          <p style="margin:0 0 17px;font-size:16px;line-height:1.7;">Thank you for your recent donation of <strong>{{DONATION_AMOUNT}}</strong> to Hope for Gaza Foundation. Your generosity contributed to our ongoing relief response for families and children facing severe hardship in Gaza.</p>
          <p style="margin:0 0 22px;font-size:16px;line-height:1.7;">Because of the compassion of donors like you, this month our team provided:</p>
          <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:0 0 22px;">
            <tr>
              <td valign="top" style="width:33.33%;padding:0 5px 0 0;"><div style="height:100%;padding:15px 9px;background:#fff8ed;border-top:4px solid #e3a42b;text-align:center;"><div style="color:#ad2436;font-size:25px;font-weight:bold;">10,000</div><div style="margin-top:6px;color:#4b4d4b;font-size:12px;line-height:1.4;">hot meals</div></div></td>
              <td valign="top" style="width:33.33%;padding:0 3px;"><div style="height:100%;padding:15px 9px;background:#f2f7ed;border-top:4px solid #5c9138;text-align:center;"><div style="color:#ad2436;font-size:25px;font-weight:bold;">50</div><div style="margin-top:6px;color:#4b4d4b;font-size:12px;line-height:1.4;">shelters for displaced families</div></div></td>
              <td valign="top" style="width:33.33%;padding:0 0 0 5px;"><div style="height:100%;padding:15px 9px;background:#f6f0f4;border-top:4px solid #ad2436;text-align:center;"><div style="color:#ad2436;font-size:25px;font-weight:bold;">1,043</div><div style="margin-top:6px;color:#4b4d4b;font-size:12px;line-height:1.4;">baby-milk formulas</div></div></td>
            </tr>
          </table>
          <p style="margin:0 0 22px;padding:14px 16px;background:#f2f7ed;border-left:4px solid #5c9138;color:#38432f;font-size:14px;line-height:1.65;"><strong>What this means:</strong> Your donation helped us respond to immediate needs with food, safe shelter, and essential nutrition for babies. We are grateful that you chose to stand with families in Gaza.</p>
          <h2 style="margin:0 0 11px;color:#202124;font-size:21px;line-height:1.35;">More families are still waiting for help</h2>
          <p style="margin:0 0 17px;font-size:15px;line-height:1.7;">We still need <strong>$10,000</strong> to reach more families in Gaza with hot meals, shelter, and baby milk. Many displaced families continue to face urgent needs, and your renewed support can help us extend this relief response.</p>
          <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:0 0 18px;background:#fff4f3;border:1px solid #f0c6c3;border-radius:7px;">
            <tr><td style="padding:19px;text-align:center;"><div style="color:#ad2436;font-size:28px;font-weight:bold;">$10,000 still needed</div><div style="margin-top:5px;color:#4b4d4b;font-size:13px;line-height:1.5;">Help us support additional families in Gaza.</div><a href="https://hope-for-gaza-foundation.fundraiseup.us.com/" style="display:inline-block;margin-top:16px;padding:13px 25px;background:#ad2436;color:#ffffff;text-decoration:none;border-radius:5px;font-size:15px;font-weight:bold;">Donate Again</a></td></tr>
          </table>
          <p style="margin:0 0 24px;text-align:center;color:#59605a;font-size:12px;line-height:1.6;">Donate securely at <a href="https://hope-for-gaza-foundation.fundraiseup.us.com/" style="color:#ad2436;font-weight:bold;">hope-for-gaza-foundation.fundraiseup.us.com</a></p>
          <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:0 0 24px;border:1px solid #dfe3e8;border-radius:7px;">
            <tr><td style="padding:15px 16px 8px;text-align:center;"><img src="https://files.manuscdn.com/user_upload_by_module/session_file/310519663541564405/YKAzCIwYHtQAAgKw.png" width="530" alt="100% Zakat Eligible. EIN 38-4401950. Verified 501(c)(3) Nonprofit Organization." style="display:block;width:100%;max-width:530px;height:auto;border:0;margin:0 auto;"></td></tr>
            <tr><td style="padding:0 16px 15px;text-align:center;color:#4b4d4b;font-size:12px;line-height:1.6;">Hope for Gaza Foundation is presented as a verified 501(c)(3) nonprofit organization, 100% Zakat eligible, EIN 38-4401950. Donations are tax-deductible and handled with full transparency, according to the organization’s information.</td></tr>
          </table>
          <p style="margin:0 0 20px;font-size:15px;line-height:1.7;">We will continue sharing updates about our relief work. If you have questions about your donation, please contact us at <a href="mailto:info@hopeforgaza.foundation" style="color:#ad2436;font-weight:bold;">info@hopeforgaza.foundation</a>.</p>
          <p style="margin:0 0 7px;font-size:16px;line-height:1.7;">With sincere gratitude,</p>
          <p style="margin:0;font-size:16px;line-height:1.6;"><strong>Hope for Gaza Foundation</strong><br><a href="mailto:info@hopeforgaza.foundation" style="color:#ad2436;">info@hopeforgaza.foundation</a><br>+1 737-282-1977</p>
        </td></tr>
        <tr><td style="padding:23px 42px;background:#202124;color:#ffffff;">
          <p style="margin:0 0 8px;font-size:15px;line-height:1.5;"><strong>Hope for Gaza Foundation</strong></p>
          <p style="margin:0 0 5px;font-size:13px;line-height:1.6;color:#e6e6e6;">7900 Balcones Drive #15017, Austin, TX 78731</p>
          <p style="margin:0;font-size:13px;line-height:1.6;color:#e6e6e6;">EIN 38-4401950 &nbsp;|&nbsp; <a href="mailto:info@hopeforgaza.foundation" style="color:#b7d88b;">info@hopeforgaza.foundation</a></p>
        </td></tr>
      </table>
      <p style="max-width:650px;margin:13px auto 0;padding:0 12px;color:#6d716b;font-size:11px;line-height:1.5;text-align:center;">You are receiving this message because you previously donated to Hope for Gaza Foundation.</p>
    </td></tr>
  </table>
</body>
</html>"""

GENERAL_THANK_YOU_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Thank You for Your Generous Support</title>
</head>
<body style="margin:0;padding:0;background:#f8fafc;color:#1e293b;font-family:Arial,sans-serif;">
  <div style="max-width:600px;margin:24px auto;background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;padding:32px;">
    <h1 style="color:#3872dc;font-size:24px;margin-top:0;">Thank You, {{DONOR_NAME}}!</h1>
    <p style="font-size:16px;line-height:1.6;color:#334155;">
      We are deeply grateful for your generous donation of <strong>{{DONATION_AMOUNT}}</strong>. Your kindness enables us to continue our vital humanitarian programs and make a lasting impact.
    </p>
    <div style="background:#f1f5f9;border-left:4px solid #3872dc;padding:16px;margin:24px 0;border-radius:4px;">
      <p style="margin:0;font-size:14px;color:#475569;">
        Every contribution directly empowers our field team to deliver emergency relief and aid to communities in urgent need.
      </p>
    </div>
    <p style="font-size:15px;line-height:1.6;color:#334155;">
      With warm regards,<br>
      <strong>Organization Team</strong>
    </p>
  </div>
</body>
</html>"""

TEMPLATES = [
    {
        "id": "hope_for_gaza_impact",
        "name": "Hope for Gaza - Relief Impact & Thank You",
        "subject": "Your kindness is making a difference in Gaza",
        "html": HOPE_FOR_GAZA_TEMPLATE_HTML,
    },
    {
        "id": "general_thank_you",
        "name": "General Donor Appreciation",
        "subject": "Thank You for Your Generous Support",
        "html": GENERAL_THANK_YOU_HTML,
    },
]


class RecipientItem(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    name: str = ""
    amount: str = ""


class SendBroadcastRequest(BaseModel):
    recipients: list[RecipientItem]
    subject: str = Field(min_length=1, max_length=250)
    html_template: str = Field(min_length=1)
    template_key: str = "hope_for_gaza_impact"
    organization_id: str | None = None
    from_name: str | None = Field(default="Hope for Gaza", max_length=150)


def _deduplicate_donors(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    donors_map: dict[str, dict[str, Any]] = {}
    for r in rows:
        raw_email = str(r.get("email") or "").strip().lower()
        if not raw_email or "@" not in raw_email or "pending@" in raw_email or "example.com" in raw_email:
            continue
        first = str(r.get("first_name") or "").strip()
        last = str(r.get("last_name") or "").strip()
        name = f"{first} {last}".strip()
        if not name or name.lower() in ("donor", "guest", "anonymous"):
            name = raw_email.split("@")[0].capitalize()

        amt = float(r.get("amount") or 0)
        curr = str(r.get("currency") or "USD").upper()
        created_at = str(r.get("created_at") or "")

        if raw_email not in donors_map:
            donors_map[raw_email] = {
                "id": r.get("id"),
                "email": raw_email,
                "first_name": first,
                "last_name": last,
                "name": name,
                "total_donated": amt,
                "donations_count": 1,
                "latest_donation_amount": amt,
                "latest_donation_currency": curr,
                "latest_donation_date": created_at,
                "campaign_id": r.get("campaign_id"),
                "organization_id": r.get("organization_id"),
                "source": "donation_history",
            }
        else:
            existing = donors_map[raw_email]
            existing["total_donated"] += amt
            existing["donations_count"] += 1
            if created_at > existing["latest_donation_date"]:
                existing["latest_donation_date"] = created_at
                existing["latest_donation_amount"] = amt
                existing["latest_donation_currency"] = curr
            if name and existing["name"] == raw_email.split("@")[0].capitalize():
                existing["name"] = name
                existing["first_name"] = first
                existing["last_name"] = last

    return list(donors_map.values())


@router.get("/donor-broadcast/templates")
def list_email_templates() -> list[dict[str, str]]:
    return TEMPLATES


@router.get("/donor-broadcast/email-history")
def get_donor_broadcast_email_history(
    email: str = Query(...),
    user: Annotated[AuthUser, Depends(require_auth)] = None,
) -> dict[str, Any]:
    from routers.admin_data import _enrich_email_history_timeline
    timeline = _enrich_email_history_timeline("", email)
    return {"email": email, "history": timeline}


@router.get("/super/donor-broadcast/donors")
def super_list_broadcast_donors(
    user: Annotated[AuthUser, Depends(require_super_admin)],
    organization_id: str | None = Query(None),
    campaign_id: str | None = Query(None),
) -> dict[str, Any]:
    params: dict[str, str] = {
        "select": "id,first_name,last_name,email,amount,currency,created_at,campaign_id,organization_id,status",
        "order": "created_at.desc",
        "limit": "100000",
    }
    if organization_id and organization_id != "all":
        params["organization_id"] = f"eq.{organization_id}"
    if campaign_id and campaign_id != "all":
        params["campaign_id"] = f"eq.{campaign_id}"

    rows = rest_get("donations", params=params) or []
    deduped = _deduplicate_donors(rows)
    return {
        "donors": deduped,
        "total_unique_donors": len(deduped),
        "total_donations_processed": len(rows),
    }


def _require_broadcast_access(user: AuthUser) -> None:
    if user.role == "super_admin":
        return
    from routers.super_admin import _BROADCAST_ACCESS_OVERVIEW
    em = user.email.strip().lower()
    if not _BROADCAST_ACCESS_OVERVIEW.get(em, False):
        raise HTTPException(
            status_code=403,
            detail="Donor Broadcast access is restricted. Super Admin must grant Broadcast Access to your profile in Team Access.",
        )


@router.get("/admin/orgs/{org_id}/donor-broadcast/donors")
def admin_list_broadcast_donors(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
    campaign_id: str | None = Query(None),
) -> dict[str, Any]:
    _require_broadcast_access(user)
    params: dict[str, str] = {
        "organization_id": f"eq.{org_id}",
        "select": "id,first_name,last_name,email,amount,currency,created_at,campaign_id,organization_id,status",
        "order": "created_at.desc",
        "limit": "100000",
    }
    if campaign_id and campaign_id != "all":
        params["campaign_id"] = f"eq.{campaign_id}"

    rows = rest_get("donations", params=params) or []
    deduped = _deduplicate_donors(rows)
    return {
        "donors": deduped,
        "total_unique_donors": len(deduped),
        "total_donations_processed": len(rows),
    }


@router.post("/super/donor-broadcast/send")
def super_send_broadcast_emails(
    payload: SendBroadcastRequest,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    if not payload.recipients:
        raise HTTPException(status_code=400, detail="No recipients selected.")

    queued_count = 0
    errors: list[str] = []

    for item in payload.recipients:
        email = item.email.strip()
        if not email or "@" not in email:
            continue
        donor_name = item.name.strip() or email.split("@")[0].capitalize()
        donation_amount = item.amount.strip() or "your donation"

        # Substitute template variables
        content = payload.html_template
        content = content.replace("{{DONOR_NAME}}", donor_name)
        content = content.replace("{{DONOR_FIRST_NAME}}", donor_name.split()[0])
        content = content.replace("{{DONATION_AMOUNT}}", donation_amount)

        try:
            # Enqueues through Resend rate-limited queue (2 per second max)
            send_resend_email(
                to=email,
                subject=payload.subject,
                html=content,
                from_name=(payload.from_name or "").strip() or "Hope for Gaza",
            )
            queued_count += 1
        except Exception as exc:
            errors.append(f"{email}: {exc}")

    return {
        "queued": queued_count,
        "total": len(payload.recipients),
        "rate_limit_per_sec": 2,
        "resend_configured": resend_configured(),
        "errors": errors,
        "message": f"Successfully queued {queued_count} emails for delivery via Resend API (rate limit: 2/sec).",
    }
