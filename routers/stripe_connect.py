from __future__ import annotations

import os
from typing import Annotated, Any
from urllib.parse import quote, urlencode

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from auth import AuthUser, deny_platform_admin_payment_writes, require_auth, require_org_access
from db import rest_delete, rest_get, rest_get_one, rest_insert, rest_insert_result, rest_patch, rest_patch_result

from frontend_url import pack_origin_token, resolve_frontend_url, unpack_origin_token

router = APIRouter(prefix="/stripe", tags=["stripe"])

STRIPE_CONNECT_CLIENT_ID = (os.getenv("STRIPE_CONNECT_CLIENT_ID", "") or "").strip().strip('"').strip("'")


def use_stripe_standard_oauth() -> bool:
    """Use Stripe OAuth account picker when a Connect client ID is configured."""
    if not STRIPE_CONNECT_CLIENT_ID:
        return False
    if os.getenv("STRIPE_CONNECT_USE_EXPRESS", "").lower() in ("1", "true", "yes"):
        return False
    if os.getenv("STRIPE_CONNECT_USE_STANDARD_OAUTH", "").lower() in ("0", "false", "no"):
        return False
    return True


def build_stripe_oauth_authorize_url(*, state: str, frontend_url: str) -> str:
    """Stripe Connect OAuth v2 — pick an existing account or create one (Fundraise Up style)."""
    params = {
        "response_type": "code",
        "client_id": STRIPE_CONNECT_CLIENT_ID,
        "scope": "read_write",
        "redirect_uri": f"{frontend_url.rstrip('/')}/api/stripe/callback",
        "state": state,
        "always_prompt": "true",
    }
    return f"https://connect.stripe.com/oauth/v2/authorize?{urlencode(params)}"

EXPRESS_ACCOUNT_CAPABILITIES = {
    "card_payments": {"requested": True},
    "transfers": {"requested": True},
}


@router.get("/connect/status")
def connect_status() -> dict[str, Any]:
    return {
        "configured": bool(STRIPE_CONNECT_CLIENT_ID),
        "redirect_uri": f"{resolve_frontend_url()}/api/stripe/callback",
        "platform_mode": not bool(STRIPE_CONNECT_CLIENT_ID),
        "oauth_mode": bool(STRIPE_CONNECT_CLIENT_ID and use_stripe_standard_oauth()),
        "oauth_version": "v2" if use_stripe_standard_oauth() else None,
    }


class ConnectStartRequest(BaseModel):
    organization_id: str
    campaign_id: str | None = None
    is_default: bool = True
    frontend_origin: str | None = None


def _resolve_stripe_account(org_id: str, campaign_id: str | None) -> str | None:
    if campaign_id:
        campaign = rest_get_one(
            "campaigns",
            params={"id": f"eq.{campaign_id}", "organization_id": f"eq.{org_id}", "select": "stripe_account_id"},
        )
        if campaign and campaign.get("stripe_account_id"):
            acct = rest_get_one(
                "stripe_accounts",
                params={"id": f"eq.{campaign['stripe_account_id']}", "select": "stripe_account_id,connection_status"},
            )
            if acct and acct.get("stripe_account_id") and acct.get("connection_status") in ("active", "pending"):
                return acct["stripe_account_id"]

    default = rest_get_one(
        "stripe_accounts",
        params={
            "organization_id": f"eq.{org_id}",
            "is_default": "eq.true",
            "select": "stripe_account_id,connection_status",
        },
    )
    if default and default.get("connection_status") in ("active", "pending"):
        return default["stripe_account_id"]
    return None


@router.post("/connect/start")
def start_connect(
    payload: ConnectStartRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, str]:
    require_org_access(payload.organization_id, user, min_role="admin")
    deny_platform_admin_payment_writes(user)

    frontend_url = resolve_frontend_url(payload.frontend_origin)

    return_path = "/admin/settings/payment-methods?connected=1"
    if payload.campaign_id:
        return_path = f"/admin/campaigns/{payload.campaign_id}/edit?step=payments&connected=1"

    return_url = f"{frontend_url}{return_path}"
    refresh_url = return_url

    if STRIPE_CONNECT_CLIENT_ID and use_stripe_standard_oauth():
        state = (
            f"{payload.organization_id}:{payload.campaign_id or ''}:"
            f"{int(payload.is_default)}:{pack_origin_token(frontend_url)}"
        )
        return {"url": build_stripe_oauth_authorize_url(state=state, frontend_url=frontend_url)}

    existing = rest_get_one(
        "stripe_accounts",
        params={
            "organization_id": f"eq.{payload.organization_id}",
            "campaign_id": f"eq.{payload.campaign_id}" if payload.campaign_id else "is.null",
            "select": "stripe_account_id",
            "order": "created_at.desc",
        },
    )
    account_id = existing.get("stripe_account_id") if existing else None

    if not account_id:
        try:
            account = stripe.Account.create(
                type="express",
                capabilities=EXPRESS_ACCOUNT_CAPABILITIES,
            )
        except stripe.error.StripeError as exc:
            raise HTTPException(
                status_code=502,
                detail=str(exc.user_message or exc),
            ) from exc
        account_id = account.id
        rest_insert(
            "stripe_accounts",
            {
                "organization_id": payload.organization_id,
                "campaign_id": payload.campaign_id or None,
                "stripe_account_id": account_id,
                "is_default": payload.is_default and not payload.campaign_id,
                "connection_status": "pending",
                "charges_enabled": False,
                "payouts_enabled": False,
            },
        )

    try:
        link = stripe.AccountLink.create(
            account=account_id,
            refresh_url=refresh_url,
            return_url=return_url,
            type="account_onboarding",
        )
    except stripe.error.StripeError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc.user_message or exc),
        ) from exc
    return {"url": link.url}


@router.get("/callback")
def stripe_callback(
    code: str = Query(...),
    state: str = Query(...),
) -> RedirectResponse:
    return RedirectResponse(url=complete_stripe_oauth(code, state))


class StripeOAuthCompletePayload(BaseModel):
    code: str
    state: str


@router.post("/oauth/complete")
def stripe_oauth_complete(payload: StripeOAuthCompletePayload) -> dict[str, str]:
    """JSON OAuth completion used by the Next.js callback proxy (avoids redirect header issues)."""
    return {"redirect_url": complete_stripe_oauth(payload.code.strip(), payload.state.strip())}


def complete_stripe_oauth(code: str, state: str) -> str:
    """Exchange the OAuth code and return the frontend redirect URL (success or error page)."""
    if state.startswith("root:"):
        from routers.payment_accounts import complete_root_stripe_oauth

        return complete_root_stripe_oauth(code, state)

    if not STRIPE_CONNECT_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Stripe Connect not configured")

    parts = state.split(":")
    org_id = parts[0]
    campaign_id = parts[1] if len(parts) > 1 and parts[1] else None
    is_default = len(parts) > 2 and parts[2] == "1"
    frontend_origin = unpack_origin_token(parts[3]) if len(parts) > 3 else None
    frontend_url = resolve_frontend_url(frontend_origin)

    def fail(message: str) -> str:
        return f"{frontend_url}/admin/settings/payment-methods?error={quote(message[:180], safe='')}"

    try:
        response = stripe.OAuth.token(grant_type="authorization_code", code=code)
    except stripe.error.StripeError as exc:
        return fail(str(exc.user_message or exc) or "Stripe authorization failed")

    stripe_account_id = getattr(response, "stripe_user_id", None)
    if not stripe_account_id:
        try:
            stripe_account_id = response["stripe_user_id"]
        except Exception:
            stripe_account_id = None
    if not stripe_account_id:
        return fail("No Stripe account returned from OAuth")

    try:
        account = stripe.Account.retrieve(stripe_account_id)
        charges_enabled = bool(getattr(account, "charges_enabled", False))
        payouts_enabled = bool(getattr(account, "payouts_enabled", False))
    except stripe.error.StripeError:
        # OAuth succeeded; account details may lag — still persist the connection.
        charges_enabled = False
        payouts_enabled = False

    payload = {
        "organization_id": org_id,
        "campaign_id": campaign_id or None,
        "stripe_account_id": stripe_account_id,
        "is_default": is_default and not campaign_id,
        "connection_status": "active" if charges_enabled else "pending",
        "charges_enabled": charges_enabled,
        "payouts_enabled": payouts_enabled,
    }

    existing = rest_get_one(
        "stripe_accounts",
        params={"stripe_account_id": f"eq.{stripe_account_id}", "select": "id"},
    )
    if existing:
        row, save_error = rest_patch_result("stripe_accounts", payload, match={"id": existing["id"]})
    else:
        row, save_error = rest_insert_result(
            "stripe_accounts",
            payload,
            on_conflict="stripe_account_id",
        )
        if not row and save_error:
            row, save_error = rest_insert_result("stripe_accounts", payload)

    if not row:
        return fail(save_error or "Unable to save Stripe account")

    row_id = row.get("id") if isinstance(row, dict) else None
    if campaign_id and row_id:
        rest_patch("campaigns", {"stripe_account_id": row_id}, match={"id": campaign_id})
    elif (is_default and not campaign_id) and row_id:
        # Org settings allow one Stripe account: replace prior org-level links.
        _replace_other_org_stripe_accounts(org_id, keep_id=str(row_id))

    redirect_path = "/admin/settings/payment-methods?connected=1"
    if campaign_id:
        redirect_path = f"/admin/campaigns/{campaign_id}/edit?step=payments&connected=1"

    return f"{frontend_url}{redirect_path}"


def _replace_other_org_stripe_accounts(org_id: str, *, keep_id: str) -> None:
    """Keep a single organization-level Stripe account (campaign-scoped rows untouched)."""
    others = rest_get(
        "stripe_accounts",
        params={
            "organization_id": f"eq.{org_id}",
            "campaign_id": "is.null",
            "select": "id",
        },
    )
    for other in others:
        other_id = str(other.get("id") or "")
        if not other_id or other_id == keep_id:
            continue
        # Clear campaign pointers that still reference the old org row.
        linked = rest_get(
            "campaigns",
            params={
                "organization_id": f"eq.{org_id}",
                "stripe_account_id": f"eq.{other_id}",
                "select": "id",
            },
        )
        for campaign in linked:
            if campaign.get("id"):
                rest_patch("campaigns", {"stripe_account_id": keep_id}, match={"id": campaign["id"]})
        rest_delete("stripe_accounts", match={"id": other_id})


@router.get("/orgs/{org_id}/accounts")
def list_stripe_accounts(
    org_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> list[dict[str, Any]]:
    require_org_access(org_id, user, min_role="member")
    return rest_get("stripe_accounts", params={"organization_id": f"eq.{org_id}", "select": "*"})


@router.delete("/accounts/{account_id}")
def disconnect_stripe_account(
    account_id: str,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, bool]:
    account = rest_get_one(
        "stripe_accounts",
        params={"id": f"eq.{account_id}", "select": "id,organization_id,campaign_id"},
    )
    if not account:
        raise HTTPException(status_code=404, detail="Stripe account not found")

    require_org_access(account["organization_id"], user, min_role="admin")
    deny_platform_admin_payment_writes(user)

    if account.get("campaign_id"):
        campaign = rest_get_one(
            "campaigns",
            params={"id": f"eq.{account['campaign_id']}", "select": "stripe_account_id"},
        )
        if campaign and campaign.get("stripe_account_id") == account_id:
            rest_patch("campaigns", {"stripe_account_id": None}, match={"id": account["campaign_id"]})

    if not rest_delete("stripe_accounts", match={"id": account_id}):
        raise HTTPException(status_code=500, detail="Unable to remove Stripe account")

    return {"removed": True}


def stripe_account_accessible(account_id: str | None) -> bool:
    """True when the platform secret key can act on this connected account."""
    if not account_id:
        return False
    try:
        stripe.Account.retrieve(account_id)
        return True
    except stripe.error.PermissionError:
        return False
    except stripe.error.InvalidRequestError as exc:
        message = str(exc).lower()
        if "does not have access" in message or "no such account" in message:
            return False
        raise
    except stripe.error.StripeError:
        return False


def get_today_stripe_account_volume(
    stripe_account_id: str | None,
    campaign_id: str | None = None,
) -> float:
    """Calculate total USD volume processed by this Stripe account today (UTC)."""
    if not stripe_account_id:
        return 0.0
    from datetime import datetime, timezone

    now_utc = datetime.now(timezone.utc)
    start_of_day = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    params: dict[str, str] = {
        "status": "in.(succeeded,processing)",
        "created_at": f"gte.{start_of_day}",
        "select": "amount,base_amount,currency,stripe_account_id",
        "limit": "5000",
    }
    if campaign_id:
        params["campaign_id"] = f"eq.{campaign_id}"

    rows = rest_get("donations", params=params)
    total = 0.0
    target_str = str(stripe_account_id).strip().lower()
    for r in rows:
        acct = str(r.get("stripe_account_id") or "").strip().lower()
        if acct == target_str:
            amt = float(r.get("base_amount") or r.get("amount") or 0.0)
            total += amt
    return total


def resolve_stripe_account_for_checkout(
    org_id: str,
    campaign_id: str,
    donation_amount: float = 0.0,
    currency: str = "USD",
) -> tuple[str | None, dict[str, Any] | None]:
    from routers.payment_accounts import (
        resolve_platform_stripe_account,
        resolve_platform_stripe_for_campaign,
        resolve_root_stripe_account,
        uses_platform_provider,
    )

    campaign = (
        rest_get_one(
            "campaigns",
            params={"id": f"eq.{campaign_id}", "select": "*"},
        )
        if campaign_id
        else None
    )

    # Check for Dual Stripe Account Waterfall mode
    sources = (campaign or {}).get("payment_account_sources") or {}
    routing_mode = sources.get("stripe_routing_mode") or "single"
    new_acct_ref = sources.get("stripe_new_account_id") or (campaign or {}).get("stripe_account_id")
    old_acct_ref = sources.get("stripe_old_account_id") or (campaign or {}).get("platform_stripe_account_id")
    raw_limit = sources.get("stripe_new_daily_limit")
    raw_per_donation = sources.get("stripe_new_per_donation_limit")
    overflow_target = str(sources.get("stripe_overflow_target") or "").strip().lower()
    try:
        daily_limit = float(raw_limit) if raw_limit is not None else 0.0
    except (ValueError, TypeError):
        daily_limit = 0.0
    try:
        per_donation_limit = float(raw_per_donation) if raw_per_donation is not None else 0.0
    except (ValueError, TypeError):
        per_donation_limit = 0.0

    if routing_mode == "dual_limit" and (daily_limit > 0 or per_donation_limit > 0):
        # Resolve new Stripe account string
        resolved_new: str | None = None
        if new_acct_ref:
            new_str = str(new_acct_ref).strip()
            if len(new_str) == 36 and "-" in new_str:
                acct_row = rest_get_one(
                    "stripe_accounts",
                    params={"id": f"eq.{new_str}", "select": "stripe_account_id"},
                )
                if acct_row and acct_row.get("stripe_account_id"):
                    resolved_new = str(acct_row["stripe_account_id"]).strip()
            elif new_str.startswith("acct_"):
                resolved_new = new_str

        if not resolved_new:
            if uses_platform_provider(org_id, "stripe", campaign_id):
                resolved_new = resolve_platform_stripe_for_campaign(campaign_id)
            else:
                resolved_new = _resolve_stripe_account(org_id, campaign_id)

        if not resolved_new:
            resolved_new = (
                _resolve_stripe_account(org_id, campaign_id)
                or resolve_platform_stripe_for_campaign(campaign_id)
                or resolve_root_stripe_account("homepage")
            )

        # Calculate today's volume on new account
        today_volume = get_today_stripe_account_volume(resolved_new, campaign_id=campaign_id) if resolved_new else 0.0

        # Check per-donation limit and daily limit
        per_donation_exceeded = bool(per_donation_limit > 0 and donation_amount > per_donation_limit)
        daily_exceeded = bool(daily_limit > 0 and today_volume >= daily_limit)

        if resolved_new and not per_donation_exceeded and not daily_exceeded:
            # Under limits -> use New Account
            return resolved_new, {
                "stripe_account": resolved_new,
                "stripe_account_type": "new",
                "daily_volume": today_volume,
                "daily_limit": daily_limit,
                "per_donation_limit": per_donation_limit,
                "donation_amount": donation_amount,
            }
        else:
            reason = "per_donation_limit_exceeded" if per_donation_exceeded else ("daily_limit_exceeded" if daily_exceeded else "fallback")
            if overflow_target == "paypal":
                return None, {
                    "stripe_account": None,
                    "stripe_account_type": "paypal_overflow",
                    "use_paypal_overflow": True,
                    "daily_volume": today_volume,
                    "daily_limit": daily_limit,
                    "per_donation_limit": per_donation_limit,
                    "donation_amount": donation_amount,
                    "reason": reason,
                }

            # Per-donation limit exceeded, daily limit reached, or new account not ready -> fallback to Old Account
            resolved_old: str | None = None
            if old_acct_ref:
                old_str = str(old_acct_ref).strip()
                if len(old_str) == 36 and "-" in old_str:
                    resolved_old = resolve_platform_stripe_account(old_str)
                    if not resolved_old:
                        acct_row = rest_get_one(
                            "stripe_accounts",
                            params={"id": f"eq.{old_str}", "select": "stripe_account_id"},
                        )
                        if acct_row and acct_row.get("stripe_account_id"):
                            resolved_old = str(acct_row["stripe_account_id"]).strip()
                elif old_str.startswith("acct_"):
                    resolved_old = old_str

            if not resolved_old:
                resolved_old = (
                    resolve_platform_stripe_for_campaign(campaign_id)
                    or resolve_root_stripe_account("homepage")
                    or _resolve_stripe_account(org_id, campaign_id)
                )

            return resolved_old, {
                "stripe_account": resolved_old,
                "stripe_account_type": "old",
                "daily_volume": today_volume,
                "daily_limit": daily_limit,
                "per_donation_limit": per_donation_limit,
                "donation_amount": donation_amount,
                "reason": reason,
            }

    if uses_platform_provider(org_id, "stripe", campaign_id):
        account_id = resolve_platform_stripe_for_campaign(campaign_id)
        if not account_id:
            return None, None
        return account_id, {"stripe_account": account_id}

    account_id = _resolve_stripe_account(org_id, campaign_id)
    if not account_id:
        return None, None
    if not stripe_account_accessible(account_id):
        return None, None
    return account_id, {"stripe_account": account_id}

