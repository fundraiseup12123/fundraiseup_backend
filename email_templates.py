from __future__ import annotations

from html import escape
from typing import Any

from email_branding import DEFAULT_EMAIL_BANNER_URL


def _fmt_amount(amount: float | str, currency: str) -> str:
    try:
        value = float(amount)
        return f"{value:,.2f} {currency.upper()}"
    except (TypeError, ValueError):
        return f"{amount} {currency.upper()}".strip()


def _campaigns_block(
    campaigns: list[dict[str, str]] | None,
    *,
    primary_color: str,
) -> str:
    if not campaigns:
        return ""

    rows: list[str] = []
    for campaign in campaigns[:4]:
        title = escape(campaign.get("title") or "Campaign")
        url = escape(campaign.get("url") or "#")
        image = (campaign.get("image_url") or "").strip()
        image_html = ""
        if image:
            image_html = f"""
              <td width="72" valign="top" style="padding:0 12px 0 0;">
                <a href="{url}" style="text-decoration:none;">
                  <img src="{escape(image)}" alt="" width="72" height="72"
                    style="display:block;width:72px;height:72px;object-fit:cover;border-radius:8px;border:0;" />
                </a>
              </td>"""
        rows.append(f"""
          <tr>
            <td style="padding:10px 0;border-bottom:1px solid #eef2f7;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                <tr>
                  {image_html}
                  <td valign="middle" style="font-size:15px;line-height:1.4;color:#0f172a;">
                    <a href="{url}" style="color:#0f172a;text-decoration:none;font-weight:600;">{title}</a>
                    <div style="margin-top:6px;">
                      <a href="{url}" style="color:{primary_color};text-decoration:none;font-size:13px;font-weight:600;">
                        Donate →
                      </a>
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>""")

    return f"""
      <tr>
        <td style="padding:8px 32px 4px;">
          <div style="font-size:13px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:#64748b;margin:0 0 8px;">
            Relevant campaigns
          </div>
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
            {''.join(rows)}
          </table>
        </td>
      </tr>"""


def branded_email_html(
    *,
    preheader: str,
    headline: str,
    body_html: str,
    cta_label: str | None = None,
    cta_url: str | None = None,
    secondary_cta_label: str | None = None,
    secondary_cta_url: str | None = None,
    logo_url: str,
    brand_name: str = "FundraiseUp",
    organization_name: str | None = None,
    primary_color: str = "#3872DC",
    banner_url: str | None = None,
    campaigns: list[dict[str, str]] | None = None,
    contact_email: str | None = None,
    unsubscribe_url: str | None = None,
    footer_extra: str | None = None,
) -> str:
    display_name = organization_name or brand_name
    banner = (banner_url or DEFAULT_EMAIL_BANNER_URL or "").strip()
    # Hosted banner <img> (not CID — Gmail turns CID into file attachments).
    # Blue cell background stays visible if the image is blocked.
    banner_block = f"""
          <tr>
            <td bgcolor="#cfe3fb" style="padding:0;background-color:#cfe3fb;text-align:center;">
              <img src="{escape(banner)}" width="560" height="280" alt=""
                style="display:block;width:100%;max-width:560px;height:auto;border:0;outline:none;text-decoration:none;" />
            </td>
          </tr>"""

    # Optional campaign logo (skip tiny platform favicon).
    logo = (logo_url or "").strip()
    show_logo = bool(logo) and "a8312bd1-f9b9-4ec1-8d28-ddb28efd9bb5" not in logo and "/icon.png" not in logo
    logo_block = ""
    if show_logo:
        logo_block = f"""
      <tr>
        <td style="padding:28px 32px 8px;text-align:center;">
          <img src="{escape(logo)}" alt="" height="56"
            style="display:inline-block;max-height:56px;width:auto;border:0;" />
        </td>
      </tr>"""

    cta_block = ""
    if cta_label and cta_url:
        cta_block += f"""
          <tr>
            <td style="padding:24px 32px 8px;text-align:center;">
              <a href="{escape(cta_url)}" style="display:inline-block;background:{primary_color};color:#ffffff;
                font-size:16px;font-weight:600;text-decoration:none;padding:14px 28px;border-radius:8px;">
                {escape(cta_label)}
              </a>
            </td>
          </tr>"""

    if secondary_cta_label and secondary_cta_url:
        cta_block += f"""
          <tr>
            <td style="padding:8px 32px 8px;text-align:center;">
              <a href="{escape(secondary_cta_url)}" style="display:inline-block;background:#ffffff;color:#334155;
                font-size:14px;font-weight:600;text-decoration:none;padding:12px 20px;border-radius:8px;
                border:1px solid #cbd5e1;">
                ✉ {escape(secondary_cta_label)}
              </a>
            </td>
          </tr>"""

    footer_bits = [
        f"{escape(display_name)} uses FundraiseUp for online giving."
    ]
    if contact_email:
        footer_bits.append(escape(contact_email))
    if footer_extra:
        footer_bits.append(escape(footer_extra))
    footer_text = " ".join(footer_bits)

    unsubscribe_block = ""
    if unsubscribe_url:
        unsubscribe_block = f"""
          <div style="margin-top:12px;">
            <a href="{escape(unsubscribe_url)}" style="color:{primary_color};text-decoration:underline;">
              Stop receiving these emails
            </a>
          </div>"""

    campaigns_html = _campaigns_block(campaigns, primary_color=primary_color)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(headline)}</title>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">
  <span style="display:none;max-height:0;overflow:hidden;">{escape(preheader)}</span>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f1f5f9;padding:32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
          style="max-width:560px;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 8px 24px rgba(15,23,42,0.08);">
          {banner_block}
          {logo_block}
          <tr>
            <td style="padding:16px 32px 8px;text-align:center;">
              <h1 style="margin:0;font-size:26px;line-height:1.35;color:#0f172a;font-weight:700;">
                {escape(headline)}
              </h1>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 32px 8px;font-size:16px;line-height:1.65;color:#334155;text-align:left;">
              {body_html}
            </td>
          </tr>
          {campaigns_html}
          {cta_block}
          <tr>
            <td style="padding:28px 32px 32px;font-size:12px;line-height:1.55;color:#64748b;text-align:center;border-top:1px solid #e2e8f0;">
              {footer_text}
              {unsubscribe_block}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def donation_confirmation_email(
    *,
    donor_name: str,
    amount: float | str,
    currency: str,
    frequency: str,
    campaign_title: str,
    logo_url: str,
    donate_url: str,
    primary_color: str = "#3872DC",
    organization_name: str | None = None,
    banner_url: str | None = None,
    contact_email: str | None = None,
) -> tuple[str, str]:
    amount_label = _fmt_amount(amount, currency)
    is_monthly = frequency == "monthly"
    subject = f"Donation confirmed — {amount_label}"
    preheader = f"Thank you! Your {amount_label} donation to {campaign_title} was received."
    name = campaign_title or organization_name or "our campaign"

    recurring_note = (
        "<p style='margin:16px 0 0;'><strong>Recurring gift:</strong> "
        "Your monthly donation is now active. You will be charged each month until you cancel.</p>"
        if is_monthly
        else ""
    )

    body = f"""
      <p style="margin:0 0 12px;">Greetings,</p>
      <p style="margin:0 0 12px;">
        Thank you for your generous {'monthly ' if is_monthly else ''}donation of
        <strong style="color:#0f172a;">{escape(amount_label)}</strong>
        to <strong>{escape(name)}</strong>.
      </p>
      <p style="margin:0 0 12px;">This email confirms your donation was processed successfully.</p>
      {recurring_note}
      <p style="margin:16px 0 0;">— Your friends at {escape(name)}</p>
    """

    html = branded_email_html(
        preheader=preheader,
        headline=f"Thank you for supporting {name}",
        body_html=body,
        cta_label=f"View {name}",
        cta_url=donate_url,
        logo_url=logo_url,
        organization_name=name,
        primary_color=primary_color,
        banner_url=banner_url,
        contact_email=contact_email,
        campaigns=None,
    )
    return subject, html


def weekly_reminder_email(
    *,
    donor_name: str | None,
    campaign_title: str,
    logo_url: str,
    donate_url: str,
    primary_color: str = "#3872DC",
    organization_name: str | None = None,
    banner_url: str | None = None,
    contact_email: str | None = None,
    unsubscribe_url: str | None = None,
) -> tuple[str, str]:
    # Prefer campaign title (dynamic from this campaign / slug) over org name.
    name = campaign_title or organization_name or "our campaign"
    subject = "👋 A friendly reminder"
    preheader = f"A friendly reminder to support {name}."

    body = f"""
      <p style="margin:0 0 12px;">Greetings,</p>
      <p style="margin:0 0 12px;">
        We are reaching out to remind you of your interest in donating to
        <strong>{escape(name)}</strong>. Should you decide to proceed, your contribution would be welcome.
        We value every donation that supports our cause.
      </p>
      <p style="margin:0;">— Your friends at {escape(name)}</p>
    """

    secondary_url = f"mailto:{contact_email}" if contact_email else None
    html = branded_email_html(
        preheader=preheader,
        headline=f"A friendly reminder to support {name}",
        body_html=body,
        cta_label=None,
        cta_url=None,
        secondary_cta_label="Contact us with any questions" if secondary_url else None,
        secondary_cta_url=secondary_url,
        logo_url=logo_url,
        organization_name=name,
        primary_color=primary_color,
        banner_url=banner_url,
        contact_email=contact_email,
        unsubscribe_url=unsubscribe_url,
        campaigns=None,
    )
    return subject, html


def popup_reminder_subscribed_email(
    *,
    campaign_title: str,
    logo_url: str,
    donate_url: str,
    primary_color: str = "#3872DC",
    organization_name: str | None = None,
    banner_url: str | None = None,
    contact_email: str | None = None,
    unsubscribe_url: str | None = None,
) -> tuple[str, str]:
    name = campaign_title or organization_name or "our campaign"
    subject = f"We'll remind you about {name}"
    body = f"""
      <p style="margin:0 0 12px;">Greetings,</p>
      <p style="margin:0 0 12px;">
        Thanks for signing up. We will send you a friendly weekly reminder about
        <strong>{escape(name)}</strong> until you donate or unsubscribe.
      </p>
      <p style="margin:0;">— Your friends at {escape(name)}</p>
    """
    secondary_url = f"mailto:{contact_email}" if contact_email else None
    html = branded_email_html(
        preheader="You're on our reminder list.",
        headline=f"A friendly reminder to support {name}",
        body_html=body,
        cta_label=None,
        cta_url=None,
        secondary_cta_label="Contact us with any questions" if secondary_url else None,
        secondary_cta_url=secondary_url,
        logo_url=logo_url,
        organization_name=name,
        primary_color=primary_color,
        banner_url=banner_url,
        contact_email=contact_email,
        unsubscribe_url=unsubscribe_url,
        campaigns=None,
    )
    return subject, html


def org_admin_invite_email(
    *,
    organization_name: str,
    role: str,
    login_url: str,
    email: str,
    temporary_password: str | None,
    existing_user: bool,
    logo_url: str,
    primary_color: str = "#3872DC",
    banner_url: str | None = None,
    contact_email: str | None = None,
) -> tuple[str, str]:
    subject = f"Your {organization_name} admin access"
    role_label = escape(role.replace("_", " ").title())

    if existing_user:
        credentials = f"""
          <p style="margin:0 0 12px;">
            Sign in with your existing FundraiseUp password using <strong>{escape(email)}</strong>.
          </p>
        """
        preheader = f"You now have {role_label} access to {organization_name}."
    else:
        credentials = f"""
          <p style="margin:0 0 12px;">Use these credentials to sign in:</p>
          <table role="presentation" cellspacing="0" cellpadding="0" style="margin:0 0 12px;">
            <tr><td style="padding:4px 0;color:#64748b;">Email</td><td style="padding:4px 0 4px 16px;"><strong>{escape(email)}</strong></td></tr>
            <tr><td style="padding:4px 0;color:#64748b;">Temporary password</td><td style="padding:4px 0 4px 16px;"><strong>{escape(temporary_password or "")}</strong></td></tr>
          </table>
          <p style="margin:0 0 12px;color:#64748b;font-size:14px;">
            Change your password after signing in from Profile settings.
          </p>
        """
        preheader = f"Your {organization_name} admin account is ready."

    body = f"""
      <p style="margin:0 0 12px;">Greetings,</p>
      <p style="margin:0 0 12px;">
        You have been added as <strong>{role_label}</strong> for
        <strong>{escape(organization_name)}</strong> on FundraiseUp.
      </p>
      {credentials}
      <p style="margin:0;">
        From your organization console you can manage campaigns, donations, team members, and settings
        for <strong>{escape(organization_name)}</strong> only.
      </p>
    """

    html = branded_email_html(
        preheader=preheader,
        headline=f"Welcome to {organization_name}",
        body_html=body,
        cta_label="Sign in to org console",
        cta_url=login_url,
        logo_url=logo_url,
        organization_name=organization_name,
        primary_color=primary_color,
        banner_url=banner_url,
        contact_email=contact_email,
    )
    return subject, html


def donation_alert_email(
    *,
    admin_name: str,
    donor_name: str,
    amount: float | str,
    currency: str,
    campaign_title: str,
    organization_name: str,
    admin_url: str,
    logo_url: str,
    primary_color: str = "#3872DC",
    banner_url: str | None = None,
    contact_email: str | None = None,
) -> tuple[str, str]:
    amount_label = _fmt_amount(amount, currency)
    subject = f"New donation — {amount_label}"
    preheader = f"{donor_name} donated {amount_label} to {campaign_title}."

    body = f"""
      <p style="margin:0 0 12px;">Greetings{', ' + escape(admin_name) if admin_name else ''},</p>
      <p style="margin:0 0 12px;">
        <strong>{escape(donor_name)}</strong> just donated
        <strong style="color:#0f172a;">{escape(amount_label)}</strong>
        to <strong>{escape(campaign_title)}</strong> ({escape(organization_name)}).
      </p>
      <p style="margin:0;">Open your organization console to view the full donation record.</p>
    """

    html = branded_email_html(
        preheader=preheader,
        headline="New donation received",
        body_html=body,
        cta_label="View in admin",
        cta_url=admin_url,
        logo_url=logo_url,
        organization_name=organization_name,
        primary_color=primary_color,
        banner_url=banner_url,
        contact_email=contact_email,
    )
    return subject, html


def weekly_digest_email(
    *,
    admin_name: str,
    organization_name: str,
    donation_count: int,
    total_raised: float,
    reporting_currency: str,
    admin_url: str,
    logo_url: str,
    primary_color: str = "#3872DC",
    banner_url: str | None = None,
    contact_email: str | None = None,
) -> tuple[str, str]:
    amount_label = _fmt_amount(total_raised, reporting_currency)
    subject = f"Weekly digest — {organization_name}"
    preheader = f"{donation_count} donations totaling {amount_label} in the last 7 days."

    body = f"""
      <p style="margin:0 0 12px;">Greetings{', ' + escape(admin_name) if admin_name else ''},</p>
      <p style="margin:0 0 12px;">Here is your weekly summary for <strong>{escape(organization_name)}</strong>:</p>
      <table role="presentation" cellspacing="0" cellpadding="0" style="margin:0 0 12px;">
        <tr>
          <td style="padding:4px 0;color:#64748b;">Donations</td>
          <td style="padding:4px 0 4px 16px;"><strong>{donation_count}</strong></td>
        </tr>
        <tr>
          <td style="padding:4px 0;color:#64748b;">Total raised</td>
          <td style="padding:4px 0 4px 16px;"><strong>{escape(amount_label)}</strong></td>
        </tr>
      </table>
      <p style="margin:0;">Open insights for charts and breakdowns.</p>
    """

    html = branded_email_html(
        preheader=preheader,
        headline=f"Your weekly insights — {organization_name}",
        body_html=body,
        cta_label="Open insights",
        cta_url=admin_url,
        logo_url=logo_url,
        organization_name=organization_name,
        primary_color=primary_color,
        banner_url=banner_url,
        contact_email=contact_email,
        campaigns=None,
    )
    return subject, html


# --- Editable org overrides (admin Templates UI) ---------------------------------

EDITABLE_TEMPLATE_KEYS: tuple[str, ...] = (
    "donation_confirmation",
    "donation_alert",
    "reminder_subscribed",
    "weekly_reminder",
    "weekly_donor_reminder",
    "org_weekly_digest",
)

TEMPLATE_LABELS: dict[str, str] = {
    "donation_confirmation": "Donation confirmation",
    "donation_alert": "Donation alert",
    "reminder_subscribed": "Reminder signup",
    "weekly_reminder": "Weekly reminder",
    "weekly_donor_reminder": "Weekly donor reminder",
    "org_weekly_digest": "Weekly digest",
}

TEMPLATE_TOKENS_HELP = (
    "{{donor_name}}, {{amount}}, {{campaign_title}}, {{org_name}}, "
    "{{admin_name}}, {{donation_count}}, {{total_raised}}"
)


def default_editable_template(template_key: str) -> dict[str, str | None]:
    """Default subject/headline/body (with tokens) for the Templates editor."""
    key = template_key.strip()
    if key == "donation_confirmation":
        return {
            "template_key": key,
            "label": TEMPLATE_LABELS[key],
            "subject": "Donation confirmed — {{amount}}",
            "headline": "Thank you for supporting {{campaign_title}}",
            "body_html": (
                "<p>Greetings,</p>"
                "<p>Thank you for your generous donation of <strong>{{amount}}</strong> "
                "to <strong>{{campaign_title}}</strong>.</p>"
                "<p>This email confirms your donation was processed successfully.</p>"
                "<p>— Your friends at {{campaign_title}}</p>"
            ),
            "banner_url": DEFAULT_EMAIL_BANNER_URL,
            "logo_url": None,
            "cta_label": "View campaign",
            "is_custom": False,
        }
    if key == "donation_alert":
        return {
            "template_key": key,
            "label": TEMPLATE_LABELS[key],
            "subject": "New donation — {{amount}}",
            "headline": "New donation received",
            "body_html": (
                "<p>Greetings{{admin_name}}.</p>"
                "<p><strong>{{donor_name}}</strong> just donated "
                "<strong>{{amount}}</strong> to <strong>{{campaign_title}}</strong> "
                "({{org_name}}).</p>"
                "<p>Open your organization console to view the full donation record.</p>"
            ),
            "banner_url": DEFAULT_EMAIL_BANNER_URL,
            "logo_url": None,
            "cta_label": "View in admin",
            "is_custom": False,
        }
    if key == "reminder_subscribed":
        return {
            "template_key": key,
            "label": TEMPLATE_LABELS[key],
            "subject": "We'll remind you about {{campaign_title}}",
            "headline": "A friendly reminder to support {{campaign_title}}",
            "body_html": (
                "<p>Greetings,</p>"
                "<p>Thanks for signing up. We will send you a friendly weekly reminder about "
                "<strong>{{campaign_title}}</strong> until you donate or unsubscribe.</p>"
                "<p>— Your friends at {{campaign_title}}</p>"
            ),
            "banner_url": DEFAULT_EMAIL_BANNER_URL,
            "logo_url": None,
            "cta_label": None,
            "is_custom": False,
        }
    if key in {"weekly_reminder", "weekly_donor_reminder"}:
        return {
            "template_key": key,
            "label": TEMPLATE_LABELS[key],
            "subject": "A friendly reminder",
            "headline": "A friendly reminder to support {{campaign_title}}",
            "body_html": (
                "<p>Greetings,</p>"
                "<p>We are reaching out to remind you of your interest in donating to "
                "<strong>{{campaign_title}}</strong>. Should you decide to proceed, your "
                "contribution would be welcome. We value every donation that supports our cause.</p>"
                "<p>— Your friends at {{campaign_title}}</p>"
            ),
            "banner_url": DEFAULT_EMAIL_BANNER_URL,
            "logo_url": None,
            "cta_label": None,
            "is_custom": False,
        }
    if key == "org_weekly_digest":
        return {
            "template_key": key,
            "label": TEMPLATE_LABELS[key],
            "subject": "Weekly digest — {{org_name}}",
            "headline": "Your weekly insights — {{org_name}}",
            "body_html": (
                "<p>Greetings{{admin_name}}.</p>"
                "<p>Here is your weekly summary for <strong>{{org_name}}</strong>:</p>"
                "<p>Donations: <strong>{{donation_count}}</strong><br/>"
                "Total raised: <strong>{{total_raised}}</strong></p>"
                "<p>Open insights for charts and breakdowns.</p>"
            ),
            "banner_url": DEFAULT_EMAIL_BANNER_URL,
            "logo_url": None,
            "cta_label": "Open insights",
            "is_custom": False,
        }
    raise ValueError(f"Unknown editable template key: {template_key}")


def apply_email_tokens(text: str, tokens: dict[str, str], *, escape_html: bool = False) -> str:
    result = text or ""
    for key, value in tokens.items():
        needle = "{{" + key + "}}"
        replacement = escape(value) if escape_html else value
        result = result.replace(needle, replacement)
    return result


def render_editable_email(
    *,
    template: dict[str, Any],
    tokens: dict[str, str],
    logo_url: str,
    primary_color: str = "#3872DC",
    organization_name: str | None = None,
    banner_url: str | None = None,
    cta_url: str | None = None,
    contact_email: str | None = None,
    unsubscribe_url: str | None = None,
) -> tuple[str, str]:
    """Build subject + HTML from an editable template row (or defaults)."""
    subject = apply_email_tokens(str(template.get("subject") or ""), tokens, escape_html=False)
    headline = apply_email_tokens(str(template.get("headline") or ""), tokens, escape_html=False)
    # Body is HTML from the editor; escape token values only.
    body = apply_email_tokens(str(template.get("body_html") or ""), tokens, escape_html=True)
    resolved_banner = (template.get("banner_url") or banner_url or DEFAULT_EMAIL_BANNER_URL or None)
    resolved_logo = (template.get("logo_url") or logo_url or "").strip() or logo_url
    cta_label = template.get("cta_label")
    cta = str(cta_label).strip() if cta_label else None

    html = branded_email_html(
        preheader=subject,
        headline=headline,
        body_html=body,
        cta_label=cta if cta_url else None,
        cta_url=cta_url if cta else None,
        logo_url=resolved_logo,
        organization_name=organization_name or tokens.get("org_name") or tokens.get("campaign_title"),
        primary_color=primary_color,
        banner_url=str(resolved_banner) if resolved_banner else None,
        contact_email=contact_email,
        unsubscribe_url=unsubscribe_url,
        campaigns=None,
    )
    return subject, html
