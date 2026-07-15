"""Public donor portal APIs — magic-link auth, history, receipts, recurring plans."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Annotated, Any, Literal
from urllib.parse import quote

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, Field

from db import rest_get, rest_get_one, rest_insert, rest_patch, supabase_enabled
from donor_auth import (
    issue_magic_link_token,
    issue_session_token,
    require_donor_email,
    verify_token,
)
from invite_service import create_supabase_user, find_user_id_by_email
from emails import resend_configured, send_resend_email
from frontend_url import resolve_frontend_url
from stripe_intents import stripe_request_kwargs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/donor", tags=["donor-portal"])

DonorEmail = Annotated[str, Depends(require_donor_email)]


class MagicLinkRequest(BaseModel):
    email: EmailStr
    origin: str | None = None


class MagicLinkResponse(BaseModel):
    ok: bool = True
    message: str
    debug_link: str | None = None


class VerifyRequest(BaseModel):
    token: str = Field(min_length=10)


class SessionResponse(BaseModel):
    token: str
    email: str
    expires_in_days: int = 14


class AccountStatusRequest(BaseModel):
    email: EmailStr


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)
    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)


class SignupStartRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    phone: str = Field(default="", max_length=40)
    password: str = Field(min_length=8, max_length=128)


class SignupVerifyRequest(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=4, max_length=8)
    password: str = Field(min_length=8, max_length=128)


class PasswordLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ProfileUpdate(BaseModel):
    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    phone: str | None = Field(default=None, max_length=40)
    address_line1: str | None = Field(default=None, max_length=120)
    address_line2: str | None = Field(default=None, max_length=120)
    city: str | None = Field(default=None, max_length=80)
    region: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=40)
    country: str | None = Field(default=None, max_length=80)


class PlanUpdateRequest(BaseModel):
    action: Literal["cancel", "pause", "resume", "skip", "change_amount", "retry"]
    amount: float | None = Field(default=None, gt=0)
    skip_months: int | None = Field(default=None, ge=1, le=12)


def _hash_secret(value: str, *, salt: str = "") -> str:
    import hashlib
    import hmac as hm

    material = f"{salt}:{value}".encode("utf-8")
    key = (os.getenv("DONOR_PORTAL_SECRET") or os.getenv("SUPABASE_SECRET_KEY") or "donor-portal").encode(
        "utf-8"
    )
    return hm.new(key, material, hashlib.sha256).hexdigest()


def _split_name(full_name: str) -> tuple[str, str]:
    parts = [p for p in full_name.strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _create_donor_profile(user_id: str, first_name: str, last_name: str) -> None:
    existing_profile = rest_get_one(
        "profiles",
        params={"id": f"eq.{user_id}", "select": "id"},
    )
    if not existing_profile:
        rest_insert(
            "profiles",
            {
                "id": user_id,
                "role": "org_user",
                "first_name": first_name or None,
                "last_name": last_name or None,
            },
        )


def _upsert_donor_contact(email: str, first_name: str, last_name: str, phone: str = "") -> None:
    existing = rest_get_one(
        "donor_profiles",
        params={"email": f"eq.{email}", "select": "email"},
    )
    row = {
        "email": email,
        "first_name": first_name or "",
        "last_name": last_name or "",
        "phone": phone or "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if existing:
        rest_patch("donor_profiles", row, match={"email": email})
    else:
        rest_insert("donor_profiles", row)



def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _debug_enabled() -> bool:
    return os.getenv("DONOR_PORTAL_DEBUG", "").strip().lower() in {"1", "true", "yes"}


def _donations_for_email(email: str) -> list[dict[str, Any]]:
    if not supabase_enabled():
        return []
    email_l = _normalize_email(email)
    # PostgREST ilike for case-insensitive match
    rows = rest_get(
        "donations",
        params={
            "select": (
                "id,first_name,last_name,email,amount,currency,frequency,payment_method,"
                "status,honoree_name,comment,created_at,campaign_id,organization_id,"
                "stripe_payment_intent_id,stripe_customer_id,stripe_subscription_id,"
                "stripe_account_id,crypto_amount,crypto_currency,base_amount"
            ),
            "email": f"ilike.{email_l}",
            "order": "created_at.desc",
            "limit": "200",
        },
    )
    if rows:
        return rows
    # Fallback exact eq in case ilike is unavailable
    return rest_get(
        "donations",
        params={
            "select": (
                "id,first_name,last_name,email,amount,currency,frequency,payment_method,"
                "status,honoree_name,comment,created_at,campaign_id,organization_id,"
                "stripe_payment_intent_id,stripe_customer_id,stripe_subscription_id,"
                "stripe_account_id,crypto_amount,crypto_currency,base_amount"
            ),
            "email": f"eq.{email_l}",
            "order": "created_at.desc",
            "limit": "200",
        },
    )


def _campaign_title(campaign_id: str | None) -> str | None:
    if not campaign_id:
        return None
    content = rest_get_one(
        "campaign_content",
        params={"campaign_id": f"eq.{campaign_id}", "select": "title"},
    )
    if content and content.get("title"):
        return str(content["title"])
    campaign = rest_get_one(
        "campaigns",
        params={"id": f"eq.{campaign_id}", "select": "name,slug"},
    )
    if campaign:
        return str(campaign.get("name") or campaign.get("slug") or "")
    return None


def _serialize_donation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "first_name": row.get("first_name"),
        "last_name": row.get("last_name"),
        "email": row.get("email"),
        "amount": float(row.get("amount") or 0),
        "currency": str(row.get("currency") or "USD").upper(),
        "frequency": row.get("frequency") or "once",
        "payment_method": row.get("payment_method"),
        "status": row.get("status") or "succeeded",
        "honoree_name": row.get("honoree_name"),
        "comment": row.get("comment"),
        "created_at": row.get("created_at"),
        "campaign_id": row.get("campaign_id"),
        "campaign_title": _campaign_title(row.get("campaign_id")),
        "crypto_amount": row.get("crypto_amount"),
        "crypto_currency": row.get("crypto_currency"),
        "has_subscription": bool(row.get("stripe_subscription_id")),
    }


def _profile_from_donations(email: str, donations: list[dict[str, Any]]) -> dict[str, Any]:
    stored = rest_get_one(
        "donor_profiles",
        params={"email": f"eq.{_normalize_email(email)}", "select": "*"},
    )
    latest = donations[0] if donations else {}
    base = {
        "email": _normalize_email(email),
        "first_name": (stored or {}).get("first_name") or latest.get("first_name") or "",
        "last_name": (stored or {}).get("last_name") or latest.get("last_name") or "",
        "phone": (stored or {}).get("phone") or "",
        "address_line1": (stored or {}).get("address_line1") or "",
        "address_line2": (stored or {}).get("address_line2") or "",
        "city": (stored or {}).get("city") or "",
        "region": (stored or {}).get("region") or "",
        "postal_code": (stored or {}).get("postal_code") or "",
        "country": (stored or {}).get("country") or "",
    }
    return base


def _magic_link_html(login_url: str, email: str) -> str:
    safe_url = escape(login_url, quote=True)
    return f"""
    <div style="font-family:system-ui,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1b2a4a">
      <h1 style="font-size:22px;margin:0 0 12px">Sign in to your Donor Portal</h1>
      <p style="line-height:1.5">Use this secure link to manage donations for <strong>{escape(email)}</strong>. It expires in 30 minutes.</p>
      <p style="margin:28px 0">
        <a href="{safe_url}" style="display:inline-block;background:#3872DC;color:#fff;text-decoration:none;padding:12px 20px;border-radius:8px;font-weight:600">
          Open Donor Portal
        </a>
      </p>
      <p style="font-size:13px;color:#6b7280;line-height:1.5">If you did not request this, you can ignore this email.</p>
    </div>
    """


@router.post("/auth/magic-link", response_model=MagicLinkResponse)
def request_magic_link(payload: MagicLinkRequest) -> MagicLinkResponse:
    email = _normalize_email(str(payload.email))
    generic = MagicLinkResponse(
        ok=True,
        message="If we find donations for this email, you’ll receive a sign-in link shortly.",
    )

    if not supabase_enabled():
        return generic

    donations = _donations_for_email(email)
    if not donations:
        return generic

    token = issue_magic_link_token(email)
    origin = resolve_frontend_url(payload.origin)
    login_url = f"{origin}/donor-portal/verify?token={quote(token)}"

    if resend_configured():
        try:
            send_resend_email(
                to=email,
                subject="Your Donor Portal sign-in link",
                html=_magic_link_html(login_url, email),
            )
        except Exception:
            logger.exception("Failed to send donor portal magic link to %s", email)
            if not _debug_enabled():
                raise HTTPException(status_code=502, detail="Unable to send sign-in email") from None

    debug_link = login_url if _debug_enabled() or not resend_configured() else None
    return MagicLinkResponse(
        ok=True,
        message=generic.message,
        debug_link=debug_link,
    )


@router.post("/auth/verify", response_model=SessionResponse)
def verify_magic_link(payload: VerifyRequest) -> SessionResponse:
    data = verify_token(payload.token, purpose="magic_link")
    email = str(data["email"])
    if not _donations_for_email(email):
        raise HTTPException(status_code=404, detail="No donations found for this email")
    session = issue_session_token(email)
    return SessionResponse(token=session, email=email)


@router.post("/auth/account-status")
def account_status(payload: AccountStatusRequest) -> dict[str, Any]:
    """Whether this email already has a FundraiseUp login (show Login vs Sign up)."""
    email = _normalize_email(str(payload.email))
    exists = bool(find_user_id_by_email(email))
    return {"email": email, "has_account": exists}


@router.post("/auth/signup/start")
def signup_start(payload: SignupStartRequest) -> dict[str, Any]:
    """Start Get started flow: store pending signup and email a 6-digit OTP."""
    import secrets

    if not supabase_enabled():
        raise HTTPException(status_code=503, detail="Signup is not configured")

    email = _normalize_email(str(payload.email))
    if find_user_id_by_email(email):
        raise HTTPException(
            status_code=409,
            detail="An account already exists for this email. Please sign in.",
        )

    otp = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    row = {
        "email": email,
        "full_name": payload.full_name.strip(),
        "phone": (payload.phone or "").strip(),
        "password_hash": _hash_secret(payload.password, salt=email),
        "otp_hash": _hash_secret(otp, salt=email),
        "attempts": 0,
        "expires_at": expires_at.isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    existing = rest_get_one(
        "donor_signup_pending",
        params={"email": f"eq.{email}", "select": "email"},
    )
    if existing:
        rest_patch("donor_signup_pending", row, match={"email": email})
    else:
        inserted = rest_insert("donor_signup_pending", row)
        if not inserted and not existing:
            raise HTTPException(
                status_code=503,
                detail="Signup storage is not ready. Run SQL migration 019_donor_signup_otp.sql.",
            )

    from email_templates import donor_otp_email

    subject, html = donor_otp_email(otp_code=otp, full_name=payload.full_name)
    debug_otp = None
    if resend_configured():
        try:
            send_resend_email(to=email, subject=subject, html=html)
        except Exception:
            logger.exception("Failed to send donor OTP to %s", email)
            if not _debug_enabled():
                raise HTTPException(status_code=502, detail="Unable to send verification email") from None
            debug_otp = otp
    else:
        debug_otp = otp

    return {
        "ok": True,
        "email": email,
        "message": "We sent a verification code to your email.",
        "debug_otp": debug_otp if _debug_enabled() or not resend_configured() else None,
    }


@router.post("/auth/signup/verify", response_model=SessionResponse)
def signup_verify(payload: SignupVerifyRequest) -> SessionResponse:
    """Verify OTP and create the FundraiseUp account."""
    from db import rest_delete

    email = _normalize_email(str(payload.email))
    pending = rest_get_one(
        "donor_signup_pending",
        params={"email": f"eq.{email}", "select": "*"},
    )
    if not pending:
        raise HTTPException(status_code=400, detail="No pending signup for this email. Start again.")

    expires_at = pending.get("expires_at")
    try:
        exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            raise HTTPException(status_code=400, detail="Code expired. Please start again.")
    except HTTPException:
        raise
    except Exception:
        pass

    attempts = int(pending.get("attempts") or 0)
    if attempts >= 5:
        raise HTTPException(status_code=429, detail="Too many attempts. Please start again.")

    otp = "".join(ch for ch in str(payload.otp) if ch.isdigit())
    if _hash_secret(otp, salt=email) != str(pending.get("otp_hash") or ""):
        rest_patch(
            "donor_signup_pending",
            {"attempts": attempts + 1},
            match={"email": email},
        )
        raise HTTPException(status_code=400, detail="Invalid verification code")

    if _hash_secret(payload.password, salt=email) != str(pending.get("password_hash") or ""):
        raise HTTPException(status_code=400, detail="Password does not match the signup form. Start again.")

    if find_user_id_by_email(email):
        raise HTTPException(status_code=409, detail="An account already exists for this email. Please sign in.")

    first_name, last_name = _split_name(str(pending.get("full_name") or ""))
    phone = str(pending.get("phone") or "")
    user_id = create_supabase_user(
        email,
        payload.password,
        first_name=first_name,
        last_name=last_name,
    )
    _create_donor_profile(user_id, first_name, last_name)
    _upsert_donor_contact(email, first_name, last_name, phone)

    try:
        rest_delete("donor_signup_pending", match={"email": email})
    except Exception:
        rest_patch(
            "donor_signup_pending",
            {"otp_hash": "used", "expires_at": datetime.now(timezone.utc).isoformat()},
            match={"email": email},
        )

    return SessionResponse(token=issue_session_token(email), email=email)


@router.post("/auth/password-login", response_model=SessionResponse)
def password_login(payload: PasswordLoginRequest) -> SessionResponse:
    """Email + password sign-in for donors who completed Get started."""
    import httpx
    from db import supabase_url, supabase_secret

    email = _normalize_email(str(payload.email))
    url = supabase_url()
    anon = (
        os.getenv("SUPABASE_PUBLISHABLE_KEY", "").strip()
        or os.getenv("SUPABASE_ANON_KEY", "").strip()
        or os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY", "").strip()
        or supabase_secret()
    )
    if not url or not anon:
        raise HTTPException(status_code=503, detail="Auth is not configured")

    response = httpx.post(
        f"{url}/auth/v1/token?grant_type=password",
        headers={
            "apikey": anon,
            "Content-Type": "application/json",
        },
        json={"email": email, "password": payload.password},
        timeout=20.0,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return SessionResponse(token=issue_session_token(email), email=email)


@router.post("/auth/register")
def register_donor_account(payload: RegisterRequest) -> dict[str, Any]:
    email = _normalize_email(str(payload.email))
    if payload.password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    if find_user_id_by_email(email):
        raise HTTPException(status_code=409, detail="An account already exists for this email. Please log in.")

    # Prefer donors who have given; still allow register after a successful donation on this device.
    donations = _donations_for_email(email)
    first_name = (payload.first_name or "").strip()
    last_name = (payload.last_name or "").strip()
    if not first_name and donations:
        first_name = str(donations[0].get("first_name") or "")
    if not last_name and donations:
        last_name = str(donations[0].get("last_name") or "")

    user_id = create_supabase_user(
        email,
        payload.password,
        first_name=first_name,
        last_name=last_name,
    )
    _create_donor_profile(user_id, first_name, last_name)

    return {
        "ok": True,
        "email": email,
        "user_id": user_id,
        "message": "Account created. You can sign in with your email and password.",
    }


@router.get("/campaigns")
def list_live_campaigns(_email: DonorEmail) -> dict[str, Any]:
    """Live campaigns for the Donor Portal browse/donate page."""
    if not supabase_enabled():
        return {"campaigns": []}

    campaigns = rest_get(
        "campaigns",
        params={
            "status": "eq.live",
            "select": "id,slug,name,organization_id,status,created_at",
            "order": "created_at.desc",
            "limit": "100",
        },
    )
    results: list[dict[str, Any]] = []
    for campaign in campaigns:
        campaign_id = campaign.get("id")
        content = rest_get_one(
            "campaign_content",
            params={
                "campaign_id": f"eq.{campaign_id}",
                "select": "title,caption,hero_url,hero_alt,logo_url,primary_color",
            },
        ) or {}
        results.append(
            {
                "id": campaign_id,
                "slug": campaign.get("slug"),
                "organization_id": campaign.get("organization_id"),
                "name": content.get("title") or campaign.get("name") or campaign.get("slug"),
                "caption": content.get("caption") or "",
                "hero_url": content.get("hero_url"),
                "hero_alt": content.get("hero_alt") or content.get("title") or "Campaign",
                "logo_url": content.get("logo_url"),
                "primary_color": content.get("primary_color") or "#3872DC",
                "status": campaign.get("status") or "live",
            }
        )
    return {"campaigns": results}


@router.get("/me")
def get_me(email: DonorEmail) -> dict[str, Any]:
    donations = _donations_for_email(email)
    return {
        "email": email,
        "profile": _profile_from_donations(email, donations),
        "donation_count": len(donations),
    }


@router.patch("/me")
def update_me(payload: ProfileUpdate, email: DonorEmail) -> dict[str, Any]:
    if not supabase_enabled():
        raise HTTPException(status_code=503, detail="Storage is not configured")

    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    patch["email"] = email
    patch["updated_at"] = datetime.now(timezone.utc).isoformat()

    existing = rest_get_one(
        "donor_profiles",
        params={"email": f"eq.{email}", "select": "email"},
    )
    if existing:
        rest_patch("donor_profiles", patch, match={"email": email})
    else:
        donations = _donations_for_email(email)
        latest = donations[0] if donations else {}
        row = {
            "email": email,
            "first_name": patch.get("first_name") or latest.get("first_name") or "",
            "last_name": patch.get("last_name") or latest.get("last_name") or "",
            "phone": patch.get("phone") or "",
            "address_line1": patch.get("address_line1") or "",
            "address_line2": patch.get("address_line2") or "",
            "city": patch.get("city") or "",
            "region": patch.get("region") or "",
            "postal_code": patch.get("postal_code") or "",
            "country": patch.get("country") or "",
            "updated_at": patch["updated_at"],
        }
        rest_insert("donor_profiles", row)

    donations = _donations_for_email(email)
    return {"email": email, "profile": _profile_from_donations(email, donations)}


@router.get("/donations")
def list_my_donations(email: DonorEmail) -> dict[str, Any]:
    donations = [_serialize_donation(row) for row in _donations_for_email(email)]
    return {"donations": donations}


def _receipt_html(row: dict[str, Any]) -> str:
    title = _campaign_title(row.get("campaign_id")) or "Donation"
    amount = float(row.get("amount") or 0)
    currency = str(row.get("currency") or "USD").upper()
    created = str(row.get("created_at") or "")[:19].replace("T", " ")
    name = f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip() or "Donor"
    method = str(row.get("payment_method") or "card").replace("_", " ").title()
    crypto = ""
    if row.get("crypto_amount") and row.get("crypto_currency"):
        crypto = f"<p>Crypto received: {escape(str(row['crypto_amount']))} {escape(str(row['crypto_currency']).upper())}</p>"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>Receipt — {escape(title)}</title>
<style>
  body {{ font-family: Georgia, serif; color: #1b2a4a; max-width: 640px; margin: 40px auto; padding: 0 20px; }}
  h1 {{ font-size: 28px; margin-bottom: 4px; }}
  .meta {{ color: #6b7280; font-size: 14px; margin-bottom: 28px; }}
  .card {{ border: 1px solid #d5d7dd; border-radius: 8px; padding: 24px; }}
  .amount {{ font-size: 32px; font-weight: 700; margin: 8px 0 20px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: 8px 0; border-bottom: 1px solid #eef0f4; font-size: 15px; }}
  td:last-child {{ text-align: right; }}
  .actions {{ margin-top: 28px; }}
  button {{ background: #3872DC; color: #fff; border: 0; border-radius: 8px; padding: 10px 16px; cursor: pointer; font-size: 14px; }}
  @media print {{ .actions {{ display: none; }} body {{ margin: 0; }} }}
</style></head><body>
  <h1>Donation receipt</h1>
  <p class="meta">Tax record for your records · {escape(created)} UTC</p>
  <div class="card">
    <div class="amount">{escape(f"{amount:,.2f} {currency}")}</div>
    <table>
      <tr><td>Donor</td><td>{escape(name)}</td></tr>
      <tr><td>Email</td><td>{escape(str(row.get("email") or ""))}</td></tr>
      <tr><td>Campaign</td><td>{escape(title)}</td></tr>
      <tr><td>Frequency</td><td>{escape(str(row.get("frequency") or "once").title())}</td></tr>
      <tr><td>Payment method</td><td>{escape(method)}</td></tr>
      <tr><td>Status</td><td>{escape(str(row.get("status") or "succeeded").title())}</td></tr>
      <tr><td>Reference</td><td>{escape(str(row.get("id") or "")[:8].upper())}</td></tr>
    </table>
    {crypto}
  </div>
  <div class="actions"><button onclick="window.print()">Print / Save as PDF</button></div>
</body></html>"""


@router.get("/donations/{donation_id}/receipt")
def get_receipt(donation_id: str, email: DonorEmail) -> HTMLResponse:
    rows = _donations_for_email(email)
    row = next((r for r in rows if str(r.get("id")) == donation_id), None)
    if not row:
        raise HTTPException(status_code=404, detail="Donation not found")
    return HTMLResponse(content=_receipt_html(row))


def _stripe_accounts_to_search(donations: list[dict[str, Any]]) -> list[str | None]:
    accounts: list[str | None] = [None]
    seen = {None}
    for row in donations:
        acct = (row.get("stripe_account_id") or "").strip() or None
        if acct not in seen:
            seen.add(acct)
            accounts.append(acct)
    return accounts


def _list_stripe_plans_for_email(email: str, donations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve recurring plans from DB subscription IDs + Stripe customer email lookup."""
    plans: dict[str, dict[str, Any]] = {}

    # 1) Prefer IDs stored on donation rows
    for row in donations:
        sub_id = (row.get("stripe_subscription_id") or "").strip()
        if not sub_id or sub_id in plans:
            continue
        acct = (row.get("stripe_account_id") or "").strip() or None
        try:
            sub = stripe.Subscription.retrieve(sub_id, **stripe_request_kwargs(acct))
            plans[sub_id] = _serialize_subscription(sub, stripe_account=acct, donation=row)
        except stripe.error.StripeError:
            logger.warning("Unable to retrieve subscription %s", sub_id)

    # 2) Stripe customer search by email for monthly donors without stored IDs
    for acct in _stripe_accounts_to_search(donations):
        try:
            customers = stripe.Customer.list(email=email, limit=10, **stripe_request_kwargs(acct))
        except stripe.error.StripeError:
            continue
        for customer in customers.data:
            try:
                subs = stripe.Subscription.list(
                    customer=customer.id,
                    status="all",
                    limit=20,
                    **stripe_request_kwargs(acct),
                )
            except stripe.error.StripeError:
                continue
            for sub in subs.data:
                if sub.id in plans:
                    continue
                plans[sub.id] = _serialize_subscription(sub, stripe_account=acct, donation=None)

    # Keep active/paused first
    ordered = sorted(
        plans.values(),
        key=lambda p: (0 if p.get("status") in {"active", "past_due", "paused", "trialing"} else 1, p.get("id") or ""),
    )
    return ordered


def _serialize_subscription(
    sub: stripe.Subscription,
    *,
    stripe_account: str | None,
    donation: dict[str, Any] | None,
) -> dict[str, Any]:
    item = sub["items"]["data"][0] if sub.get("items") and sub["items"]["data"] else None
    price = item["price"] if item else None
    unit_amount = (price.get("unit_amount") or 0) if price else 0
    currency = ((price.get("currency") if price else None) or "usd").upper()
    amount = unit_amount / 100.0
    pause = sub.get("pause_collection") or {}
    status = sub.get("status") or "unknown"
    if pause:
        status = "paused"

    campaign_title = None
    meta = sub.get("metadata") or {}
    if donation:
        campaign_title = _campaign_title(donation.get("campaign_id"))
    elif meta.get("campaign_id"):
        campaign_title = _campaign_title(meta.get("campaign_id"))

    return {
        "id": sub.id,
        "status": status,
        "amount": amount,
        "currency": currency,
        "customer_id": sub.customer if isinstance(sub.customer, str) else getattr(sub.customer, "id", None),
        "stripe_account_id": stripe_account,
        "current_period_end": datetime.fromtimestamp(sub.current_period_end, tz=timezone.utc).isoformat()
        if sub.get("current_period_end")
        else None,
        "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
        "paused_until": datetime.fromtimestamp(pause["resumes_at"], tz=timezone.utc).isoformat()
        if pause.get("resumes_at")
        else None,
        "campaign_title": campaign_title or meta.get("campaign_slug") or "Monthly donation",
        "default_payment_method": sub.get("default_payment_method"),
    }


@router.get("/plans")
def list_plans(email: DonorEmail) -> dict[str, Any]:
    donations = _donations_for_email(email)
    plans = _list_stripe_plans_for_email(email, donations)
    return {"plans": plans}


def _find_plan(email: str, subscription_id: str) -> dict[str, Any]:
    donations = _donations_for_email(email)
    plans = _list_stripe_plans_for_email(email, donations)
    plan = next((p for p in plans if p["id"] == subscription_id), None)
    if not plan:
        raise HTTPException(status_code=404, detail="Recurring plan not found")
    return plan


@router.post("/plans/{subscription_id}")
def update_plan(subscription_id: str, payload: PlanUpdateRequest, email: DonorEmail) -> dict[str, Any]:
    plan = _find_plan(email, subscription_id)
    acct = plan.get("stripe_account_id")
    kwargs = stripe_request_kwargs(acct)

    try:
        if payload.action == "cancel":
            sub = stripe.Subscription.modify(
                subscription_id,
                cancel_at_period_end=True,
                **kwargs,
            )
        elif payload.action == "resume":
            sub = stripe.Subscription.modify(
                subscription_id,
                pause_collection="",
                cancel_at_period_end=False,
                **kwargs,
            )
        elif payload.action == "pause":
            resumes = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
            sub = stripe.Subscription.modify(
                subscription_id,
                pause_collection={"behavior": "void", "resumes_at": resumes},
                **kwargs,
            )
        elif payload.action == "skip":
            months = payload.skip_months or 1
            resumes = int((datetime.now(timezone.utc) + timedelta(days=30 * months)).timestamp())
            sub = stripe.Subscription.modify(
                subscription_id,
                pause_collection={"behavior": "void", "resumes_at": resumes},
                **kwargs,
            )
        elif payload.action == "change_amount":
            if not payload.amount:
                raise HTTPException(status_code=400, detail="amount is required")
            sub = stripe.Subscription.retrieve(subscription_id, **kwargs)
            item = sub["items"]["data"][0]
            old_price = item["price"]
            product = old_price["product"]
            if isinstance(product, dict):
                product = product["id"]
            new_price = stripe.Price.create(
                unit_amount=int(round(payload.amount * 100)),
                currency=old_price["currency"],
                recurring={"interval": "month"},
                product=product,
                **kwargs,
            )
            sub = stripe.Subscription.modify(
                subscription_id,
                items=[{"id": item["id"], "price": new_price.id}],
                proration_behavior="none",
                **kwargs,
            )
        elif payload.action == "retry":
            open_invoices = stripe.Invoice.list(
                subscription=subscription_id,
                status="open",
                limit=1,
                **kwargs,
            )
            if not open_invoices.data:
                raise HTTPException(status_code=400, detail="No open invoice to retry")
            stripe.Invoice.pay(open_invoices.data[0].id, **kwargs)
            sub = stripe.Subscription.retrieve(subscription_id, **kwargs)
        else:
            raise HTTPException(status_code=400, detail="Unknown action")
    except stripe.error.StripeError as exc:
        raise HTTPException(status_code=400, detail=str(exc.user_message or exc)) from exc

    return {
        "plan": _serialize_subscription(sub, stripe_account=acct, donation=None),
    }


@router.post("/plans/{subscription_id}/billing-portal")
def billing_portal(subscription_id: str, email: DonorEmail, origin: str | None = Query(default=None)) -> dict[str, str]:
    plan = _find_plan(email, subscription_id)
    customer_id = plan.get("customer_id")
    if not customer_id:
        raise HTTPException(status_code=400, detail="No Stripe customer on this plan")
    acct = plan.get("stripe_account_id")
    return_url = f"{resolve_frontend_url(origin)}/donor-portal/plans"
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
            **stripe_request_kwargs(acct),
        )
    except stripe.error.StripeError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc.user_message or exc)
            or "Stripe Customer Portal is not enabled for this account",
        ) from exc
    return {"url": session.url}


@router.get("/health")
def donor_health() -> dict[str, str]:
    return {"status": "ok"}
