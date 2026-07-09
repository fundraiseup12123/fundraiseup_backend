from __future__ import annotations

from html import escape


def _fmt_amount(amount: float | str, currency: str) -> str:
    try:
        value = float(amount)
        return f"{value:,.2f} {currency.upper()}"
    except (TypeError, ValueError):
        return f"{amount} {currency.upper()}".strip()


def branded_email_html(
    *,
    preheader: str,
    headline: str,
    body_html: str,
    cta_label: str | None = None,
    cta_url: str | None = None,
    logo_url: str,
    brand_name: str = "Fundraise",
    primary_color: str = "#3872DC",
) -> str:
    cta_block = ""
    if cta_label and cta_url:
        cta_block = f"""
          <tr>
            <td style="padding:28px 32px 8px;text-align:center;">
              <a href="{escape(cta_url)}" style="display:inline-block;background:{primary_color};color:#ffffff;
                font-size:16px;font-weight:600;text-decoration:none;padding:14px 28px;border-radius:8px;">
                {escape(cta_label)}
              </a>
            </td>
          </tr>"""

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
          <tr>
            <td style="background:{primary_color};padding:24px 32px;text-align:center;">
              <img src="{escape(logo_url)}" alt="{escape(brand_name)}" height="40"
                style="display:block;margin:0 auto 12px;max-height:40px;width:auto;" />
              <div style="color:#ffffff;font-size:13px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;">
                {escape(brand_name)}
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding:32px 32px 8px;">
              <h1 style="margin:0 0 16px;font-size:24px;line-height:1.3;color:#0f172a;">{escape(headline)}</h1>
              <div style="font-size:16px;line-height:1.6;color:#334155;">{body_html}</div>
            </td>
          </tr>
          {cta_block}
          <tr>
            <td style="padding:24px 32px 32px;font-size:12px;line-height:1.5;color:#64748b;text-align:center;">
              You received this email from {escape(brand_name)} because of activity on our donation platform.
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
) -> tuple[str, str]:
    amount_label = _fmt_amount(amount, currency)
    is_monthly = frequency == "monthly"
    subject = f"Donation confirmed — {amount_label}"
    preheader = f"Thank you! Your {amount_label} donation to {campaign_title} was received."

    recurring_note = (
        "<p style='margin:16px 0 0;'><strong>Recurring gift:</strong> "
        "Your monthly donation is now active. You will be charged each month until you cancel.</p>"
        if is_monthly
        else ""
    )

    body = f"""
      <p style="margin:0 0 12px;">Hi {escape(donor_name or 'Friend')},</p>
      <p style="margin:0 0 12px;">
        Thank you for your generous {'monthly ' if is_monthly else ''}donation of
        <strong style="color:#0f172a;">{escape(amount_label)}</strong>
        to <strong>{escape(campaign_title)}</strong>.
      </p>
      <p style="margin:0;">This email confirms your payment was processed successfully.</p>
      {recurring_note}
    """

    html = branded_email_html(
        preheader=preheader,
        headline="Thank you for your donation",
        body_html=body,
        cta_label="View campaign",
        cta_url=donate_url,
        logo_url=logo_url,
        primary_color=primary_color,
    )
    return subject, html


def weekly_reminder_email(
    *,
    donor_name: str | None,
    campaign_title: str,
    logo_url: str,
    donate_url: str,
    primary_color: str = "#3872DC",
) -> tuple[str, str]:
    subject = f"Your support still matters — {campaign_title}"
    preheader = f"A gentle reminder to support {campaign_title}."
    greeting = escape(donor_name) if donor_name else "there"

    body = f"""
      <p style="margin:0 0 12px;">Hi {greeting},</p>
      <p style="margin:0 0 12px;">
        Families supported by <strong>{escape(campaign_title)}</strong> still need your help.
        If you are able, please consider making a gift today.
      </p>
      <p style="margin:0;">Every contribution makes a real difference.</p>
    """

    html = branded_email_html(
        preheader=preheader,
        headline="A gentle reminder",
        body_html=body,
        cta_label="Donate now",
        cta_url=donate_url,
        logo_url=logo_url,
        primary_color=primary_color,
    )
    return subject, html


def popup_reminder_subscribed_email(
    *,
    campaign_title: str,
    logo_url: str,
    donate_url: str,
    primary_color: str = "#3872DC",
) -> tuple[str, str]:
    subject = f"We'll remind you about {campaign_title}"
    body = f"""
      <p style="margin:0 0 12px;">Thanks for signing up.</p>
      <p style="margin:0;">
        We will send you a gentle weekly reminder about <strong>{escape(campaign_title)}</strong>
        until you donate or unsubscribe.
      </p>
    """
    html = branded_email_html(
        preheader="You're on our reminder list.",
        headline="Reminder saved",
        body_html=body,
        cta_label="Donate now",
        cta_url=donate_url,
        logo_url=logo_url,
        primary_color=primary_color,
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
) -> tuple[str, str]:
    subject = f"Your {escape(organization_name)} admin access"
    role_label = escape(role.replace("_", " ").title())

    if existing_user:
        credentials = f"""
          <p style="margin:0 0 12px;">
            Sign in with your existing Fundraise password using <strong>{escape(email)}</strong>.
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
      <p style="margin:0 0 12px;">Hello,</p>
      <p style="margin:0 0 12px;">
        You have been added as <strong>{role_label}</strong> for
        <strong>{escape(organization_name)}</strong> on Fundraise.
      </p>
      {credentials}
      <p style="margin:0;">
        From your organization console you can manage campaigns, donations, team members, and settings
        for <strong>{escape(organization_name)}</strong> only.
      </p>
    """

    html = branded_email_html(
        preheader=preheader,
        headline=f"Welcome to {escape(organization_name)}",
        body_html=body,
        cta_label="Sign in to org console",
        cta_url=login_url,
        logo_url=logo_url,
        primary_color=primary_color,
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
) -> tuple[str, str]:
    amount_label = _fmt_amount(amount, currency)
    subject = f"New donation — {amount_label}"
    preheader = f"{donor_name} donated {amount_label} to {campaign_title}."

    body = f"""
      <p style="margin:0 0 12px;">Hi {escape(admin_name or 'there')},</p>
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
        primary_color=primary_color,
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
) -> tuple[str, str]:
    amount_label = _fmt_amount(total_raised, reporting_currency)
    subject = f"Weekly digest — {organization_name}"
    preheader = f"{donation_count} donations totaling {amount_label} in the last 7 days."

    body = f"""
      <p style="margin:0 0 12px;">Hi {escape(admin_name or 'there')},</p>
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
        headline="Your weekly insights digest",
        body_html=body,
        cta_label="Open insights",
        cta_url=admin_url,
        logo_url=logo_url,
        primary_color=primary_color,
    )
    return subject, html
