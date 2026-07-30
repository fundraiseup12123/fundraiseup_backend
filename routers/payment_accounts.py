from __future__ import annotations

import json
import os
import uuid
from typing import Annotated, Any, Literal
from urllib.parse import quote

import stripe
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from auth import AuthUser, require_super_admin
from db import rest_get_one, rest_insert, rest_patch
from site_constants import ROOT_CAMPAIGN_ID

from frontend_url import pack_origin_token, resolve_frontend_url, unpack_origin_token
from routers.stripe_connect import (
    STRIPE_CONNECT_CLIENT_ID,
    build_stripe_oauth_authorize_url,
    use_stripe_standard_oauth,
)

router = APIRouter(prefix="/super/payment-accounts", tags=["payment-accounts"])

PaymentView = Literal["homepage", "popup"]

EXPRESS_ACCOUNT_CAPABILITIES = {
    "card_payments": {"requested": True},
    "transfers": {"requested": True},
}


class PaymentViewPayload(BaseModel):
    view: PaymentView
    frontend_origin: str | None = None


class StripePoolConnectPayload(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    account_entry_id: str | None = None
    view: PaymentView | None = None
    frontend_origin: str | None = None


class StripePoolEntryPayload(BaseModel):
    account_entry_id: str


class StripeDisconnectPayload(BaseModel):
    account_entry_id: str | None = None
    view: PaymentView | None = None


class StripePoolRenamePayload(BaseModel):
    account_entry_id: str
    name: str = Field(min_length=1, max_length=120)


class PaymentAccountView(BaseModel):
    view: PaymentView
    stripe_account_id: str | None = None
    stripe_connection_status: str | None = None
    stripe_charges_enabled: bool = False
    paypal_merchant_id: str | None = None
    paypal_connection_status: str | None = None


def _default_view_entry() -> dict[str, Any]:
    return {
        "stripe_account_id": None,
        "stripe_connection_status": None,
        "stripe_charges_enabled": False,
        "paypal_merchant_id": None,
        "paypal_connection_status": None,
        "nowpayments_api_key": None,
        "nowpayments_ipn_secret": None,
        "nowpayments_api_key_hint": None,
        "nowpayments_connection_status": None,
    }


def _default_accounts() -> dict[str, Any]:
    return {
        "homepage": _default_view_entry(),
        "popup": _default_view_entry(),
        "stripe_accounts": [],
    }


def _normalize_stripe_pool_entry(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    stripe_account_id = str(raw.get("stripe_account_id") or "").strip() or None
    entry_id = str(raw.get("id") or "").strip() or str(uuid.uuid4())
    name = str(raw.get("name") or "").strip() or "Stripe account"
    status = str(raw.get("connection_status") or raw.get("stripe_connection_status") or "").strip() or None
    return {
        "id": entry_id,
        "name": name[:120],
        "stripe_account_id": stripe_account_id,
        "connection_status": status,
        "charges_enabled": bool(raw.get("charges_enabled") or raw.get("stripe_charges_enabled")),
        "is_default": bool(raw.get("is_default")),
    }


def _sync_default_stripe_to_views(accounts: dict[str, Any]) -> None:
    """Keep homepage Stripe fields aligned with the pool default (legacy campaign + homepage checkout)."""
    pool = accounts.get("stripe_accounts")
    if not isinstance(pool, list):
        pool = []
    default = next((e for e in pool if isinstance(e, dict) and e.get("is_default")), None)
    if not default:
        default = next((e for e in pool if isinstance(e, dict) and e.get("stripe_account_id")), None)
    homepage = accounts.get("homepage")
    if not isinstance(homepage, dict):
        return
    if default and default.get("stripe_account_id"):
        homepage["stripe_account_id"] = default.get("stripe_account_id")
        homepage["stripe_connection_status"] = default.get("connection_status")
        homepage["stripe_charges_enabled"] = bool(default.get("charges_enabled"))
    else:
        homepage["stripe_account_id"] = None
        homepage["stripe_connection_status"] = None
        homepage["stripe_charges_enabled"] = False


def _migrate_legacy_stripe_into_pool(accounts: dict[str, Any]) -> bool:
    """Seed named pool from saved homepage/popup Stripe. Current homepage becomes default."""
    changed = False
    pool = accounts.get("stripe_accounts")
    if not isinstance(pool, list):
        pool = []
        accounts["stripe_accounts"] = pool
        changed = True

    normalized: list[dict[str, Any]] = []
    seen_acct: set[str] = set()
    for raw in pool:
        entry = _normalize_stripe_pool_entry(raw)
        if not entry:
            changed = True
            continue
        acct = entry.get("stripe_account_id")
        if acct and acct in seen_acct:
            changed = True
            continue
        if acct:
            seen_acct.add(acct)
        normalized.append(entry)
    if len(normalized) != len(pool):
        changed = True
    pool = normalized
    accounts["stripe_accounts"] = pool

    def _add_from_view(view: PaymentView, default_name: str, make_default: bool) -> None:
        nonlocal changed
        entry = accounts.get(view) if isinstance(accounts.get(view), dict) else {}
        acct = str((entry or {}).get("stripe_account_id") or "").strip()
        if not acct or acct in seen_acct:
            return
        pool.append(
            {
                "id": str(uuid.uuid4()),
                "name": default_name,
                "stripe_account_id": acct,
                "connection_status": (entry or {}).get("stripe_connection_status"),
                "charges_enabled": bool((entry or {}).get("stripe_charges_enabled")),
                "is_default": make_default,
            }
        )
        seen_acct.add(acct)
        changed = True

    has_default = any(e.get("is_default") for e in pool)
    _add_from_view("homepage", "Default", make_default=not has_default and not pool)
    # If homepage existed and we just added it alone, it is already default.
    if pool and not any(e.get("is_default") for e in pool):
        homepage_acct = str((accounts.get("homepage") or {}).get("stripe_account_id") or "").strip()
        matched = next((e for e in pool if e.get("stripe_account_id") == homepage_acct), None)
        (matched or pool[0])["is_default"] = True
        changed = True
    _add_from_view("popup", "Pop-up", make_default=not any(e.get("is_default") for e in pool))

    # Exactly one default when the pool is non-empty.
    defaults = [e for e in pool if e.get("is_default")]
    if pool and len(defaults) != 1:
        for e in pool:
            e["is_default"] = False
        homepage_acct = str((accounts.get("homepage") or {}).get("stripe_account_id") or "").strip()
        matched = next((e for e in pool if e.get("stripe_account_id") == homepage_acct), None)
        (matched or pool[0])["is_default"] = True
        changed = True

    return changed


def _ensure_stripe_pool(accounts: dict[str, Any], *, persist: bool = False) -> list[dict[str, Any]]:
    changed = _migrate_legacy_stripe_into_pool(accounts)
    if changed and persist:
        _sync_default_stripe_to_views(accounts)
        _save_accounts(accounts)
    return [e for e in accounts.get("stripe_accounts") or [] if isinstance(e, dict)]


def _public_stripe_pool(accounts: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": e.get("id"),
            "name": e.get("name") or "Stripe account",
            "stripe_account_id": e.get("stripe_account_id"),
            "connection_status": e.get("connection_status"),
            "charges_enabled": bool(e.get("charges_enabled")),
            "is_default": bool(e.get("is_default")),
        }
        for e in _ensure_stripe_pool(accounts)
    ]


def _find_pool_entry(accounts: dict[str, Any], entry_id: str) -> dict[str, Any] | None:
    for entry in _ensure_stripe_pool(accounts):
        if str(entry.get("id")) == str(entry_id):
            return entry
    return None


def _set_pool_default(accounts: dict[str, Any], entry_id: str) -> dict[str, Any]:
    pool = _ensure_stripe_pool(accounts)
    target = next((e for e in pool if str(e.get("id")) == str(entry_id)), None)
    if not target:
        raise HTTPException(status_code=404, detail="Stripe account not found")
    for entry in pool:
        entry["is_default"] = str(entry.get("id")) == str(entry_id)
    # Explicit default change updates both public views.
    for view in ("homepage", "popup"):
        view_entry = accounts.get(view)
        if not isinstance(view_entry, dict):
            continue
        view_entry["stripe_account_id"] = target.get("stripe_account_id")
        view_entry["stripe_connection_status"] = target.get("connection_status")
        view_entry["stripe_charges_enabled"] = bool(target.get("charges_enabled"))
    return target


def _load_accounts_raw() -> dict[str, Any]:
    content = rest_get_one(
        "campaign_content",
        params={"campaign_id": f"eq.{ROOT_CAMPAIGN_ID}", "select": "payment_accounts_json"},
    )
    raw = (content or {}).get("payment_accounts_json")
    if not raw:
        return _default_accounts()
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(parsed, dict):
            return _default_accounts()
        merged = _default_accounts()
        for view in ("homepage", "popup"):
            if isinstance(parsed.get(view), dict):
                merged[view].update(parsed[view])
        if isinstance(parsed.get("stripe_accounts"), list):
            merged["stripe_accounts"] = parsed["stripe_accounts"]
        if _migrate_legacy_stripe_into_pool(merged):
            _sync_default_stripe_to_views(merged)
            try:
                _save_accounts(merged)
            except HTTPException:
                pass
        return merged
    except (json.JSONDecodeError, TypeError):
        return _default_accounts()


def _save_accounts(accounts: dict[str, Any]) -> None:
    payload = {"payment_accounts_json": json.dumps(accounts)}
    result = rest_patch(
        "campaign_content",
        payload,
        match={"campaign_id": ROOT_CAMPAIGN_ID},
    )
    if result is not None:
        return

    inserted = rest_insert(
        "campaign_content",
        {"campaign_id": ROOT_CAMPAIGN_ID, **payload},
    )
    if not inserted:
        raise HTTPException(
            status_code=503,
            detail="Could not save payment accounts. Run backend/sql/006_payment_accounts_json.sql in Supabase.",
        )


def _refresh_stripe_view(view: PaymentView, accounts: dict[str, Any]) -> None:
    account_id = accounts[view].get("stripe_account_id")
    if not account_id:
        return
    try:
        account = stripe.Account.retrieve(account_id)
        accounts[view]["stripe_connection_status"] = "active" if account.charges_enabled else "pending"
        accounts[view]["stripe_charges_enabled"] = bool(account.charges_enabled)
    except stripe.error.StripeError:
        accounts[view]["stripe_connection_status"] = "restricted"


def _refresh_stripe_pool(accounts: dict[str, Any]) -> None:
    for entry in _ensure_stripe_pool(accounts):
        account_id = entry.get("stripe_account_id")
        if not account_id:
            continue
        try:
            account = stripe.Account.retrieve(account_id)
            entry["connection_status"] = "active" if account.charges_enabled else "pending"
            entry["charges_enabled"] = bool(account.charges_enabled)
        except stripe.error.StripeError:
            entry["connection_status"] = "restricted"
    _sync_default_stripe_to_views(accounts)


def _accounts_response(accounts: dict[str, Any]) -> dict[str, Any]:
    views: list[dict[str, Any]] = []
    for view in ("homepage", "popup"):
        entry = dict(accounts[view])
        entry.pop("nowpayments_api_key", None)
        entry.pop("nowpayments_ipn_secret", None)
        views.append({"view": view, **entry})
    return {
        "views": views,
        "stripe_accounts": _public_stripe_pool(accounts),
    }


@router.get("/status")
def payment_accounts_status(
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    from paypal_client import paypal_env

    paypal_return_uri = f"{resolve_frontend_url()}/api/paypal/callback"
    stripe_callback = f"{resolve_frontend_url()}/api/stripe/callback"
    return {
        "stripe_configured": bool(stripe.api_key),
        "paypal_configured": bool(
            os.getenv("PAYPAL_CLIENT_ID") or os.getenv("NEXT_PUBLIC_PAYPAL_CLIENT_ID")
        ),
        "paypal_env": paypal_env(),
        "paypal_connect_mode": "partner_or_email",
        "stripe_redirect_uri": stripe_callback,
        "stripe_connect_mode": "oauth_v2" if use_stripe_standard_oauth() else "express",
        "stripe_setup_hint": (
            "Stripe Dashboard (Live mode) → Settings → Connect → Onboarding options → OAuth: "
            "enable Standard OAuth, then add this Redirect URI: "
            f"{stripe_callback}"
        ) if use_stripe_standard_oauth() else None,
        "paypal_redirect_uri": paypal_return_uri,
        "paypal_setup_hint": (
            "PayPal Partner onboarding requires a PayPal Commerce Platform partner account. "
            "If partner connect is unavailable, Connect PayPal will ask for the business email "
            "that should receive donations. Optional partner Return URL: "
            f"{paypal_return_uri}"
        ),
        "nowpayments_setup_hint": (
            "Paste your NOWPayments API key and IPN secret from Store Settings. "
            "Crypto checkout works on all devices once attached."
        ),
    }


@router.get("")
def list_payment_accounts(
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    accounts = _load_accounts_raw()
    _refresh_stripe_pool(accounts)
    for view in ("homepage", "popup"):
        _refresh_stripe_view(view, accounts)  # type: ignore[arg-type]
    _sync_default_stripe_to_views(accounts)
    _save_accounts(accounts)
    return _accounts_response(accounts)


def resolve_root_stripe_account(checkout_view: str | None) -> str | None:
    from routers.stripe_connect import stripe_account_accessible

    view: PaymentView = "popup" if checkout_view == "popup" else "homepage"
    accounts = _load_accounts_raw()
    if view == "homepage":
        pool = _ensure_stripe_pool(accounts)
        default = next((e for e in pool if e.get("is_default")), None) or (pool[0] if pool else None)
        if default and default.get("stripe_account_id"):
            account_id = default["stripe_account_id"]
            if default.get("connection_status") not in ("active", "pending", None):
                return None
            if not stripe_account_accessible(account_id):
                return None
            return account_id
    entry = accounts.get(view, {})
    account_id = entry.get("stripe_account_id")
    if not account_id:
        return None
    if entry.get("stripe_connection_status") not in ("active", "pending", None):
        return None
    if not stripe_account_accessible(account_id):
        return None
    return account_id


def resolve_platform_stripe_account(entry_id: str | None = None) -> str | None:
    """Resolve a named platform Stripe account, falling back to the pool default."""
    from routers.stripe_connect import stripe_account_accessible

    accounts = _load_accounts_raw()
    pool = _ensure_stripe_pool(accounts)
    entry: dict[str, Any] | None = None
    if entry_id:
        entry = next((e for e in pool if str(e.get("id")) == str(entry_id)), None)
    if not entry:
        entry = next((e for e in pool if e.get("is_default")), None) or (pool[0] if pool else None)
    if not entry or not entry.get("stripe_account_id"):
        return resolve_root_stripe_account("homepage")
    if entry.get("connection_status") not in ("active", "pending", None):
        return None
    account_id = entry["stripe_account_id"]
    if not stripe_account_accessible(account_id):
        return None
    return account_id


def resolve_platform_stripe_for_campaign(campaign_id: str | None) -> str | None:
    entry_id = None
    if campaign_id:
        campaign = rest_get_one(
            "campaigns",
            params={"id": f"eq.{campaign_id}", "select": "platform_stripe_account_id"},
        )
        entry_id = (campaign or {}).get("platform_stripe_account_id")
    return resolve_platform_stripe_account(entry_id)


def resolve_root_paypal_payee(checkout_view: str | None) -> str | None:
    view: PaymentView = "popup" if checkout_view == "popup" else "homepage"
    accounts = _load_accounts_raw()
    entry = accounts.get(view, {})
    email = entry.get("paypal_email")
    merchant = entry.get("paypal_merchant_id")
    if not email and not merchant:
        return None
    status = entry.get("paypal_connection_status")
    if status and status not in ("active", "pending", "connected", None):
        return None
    if email and "@" in str(email):
        return str(email).strip()
    if merchant and "@" in str(merchant):
        return str(merchant).strip()
    return None


def normalize_payment_account_sources(raw: object) -> dict[str, str]:
    defaults = {"stripe": "organization", "paypal": "organization", "nowpayments": "organization"}
    data = raw
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            data = None
    if not isinstance(data, dict):
        return defaults
    normalized = dict(defaults)
    for key in defaults:
        value = str(data.get(key) or "").strip().lower()
        if value in {"platform", "organization"}:
            normalized[key] = value
    return normalized


def resolve_payment_account_sources(
    org_id: str | None,
    campaign_id: str | None,
) -> dict[str, str]:
    defaults = {"stripe": "organization", "paypal": "organization", "nowpayments": "organization"}
    resolved_org_id = org_id
    if campaign_id:
        campaign = rest_get_one(
            "campaigns",
            params={"id": f"eq.{campaign_id}", "select": "payment_account_sources,organization_id"},
        )
        if campaign:
            if not resolved_org_id:
                resolved_org_id = campaign.get("organization_id")
            raw = campaign.get("payment_account_sources")
            if raw is not None:
                return normalize_payment_account_sources(raw)
    if resolved_org_id:
        org = rest_get_one(
            "organizations",
            params={"id": f"eq.{resolved_org_id}", "select": "payment_account_sources"},
        )
        return normalize_payment_account_sources((org or {}).get("payment_account_sources"))
    return defaults


def uses_platform_provider(
    org_id: str | None,
    provider: str,
    campaign_id: str | None = None,
) -> bool:
    sources = resolve_payment_account_sources(org_id, campaign_id)
    return str(sources.get(provider) or "").strip().lower() == "platform"


def org_uses_platform_provider(org_id: str | None, provider: str) -> bool:
    return uses_platform_provider(org_id, provider, None)


def homepage_payment_summary() -> dict[str, Any]:
    accounts = _load_accounts_raw()
    entry = accounts.get("homepage") or {}
    pool = _public_stripe_pool(accounts)
    default = next((e for e in pool if e.get("is_default")), None) or (pool[0] if pool else None)
    stripe_id = (default or {}).get("stripe_account_id") or entry.get("stripe_account_id")
    paypal_merchant = entry.get("paypal_merchant_id")
    paypal_email = entry.get("paypal_email")
    now_hint = entry.get("nowpayments_api_key_hint")
    now_key = entry.get("nowpayments_api_key")
    return {
        "stripe": {
            "connected": bool(stripe_id) or bool(pool),
            "stripe_account_id": stripe_id,
            "connection_status": (default or {}).get("connection_status")
            or entry.get("stripe_connection_status"),
            "charges_enabled": bool(
                (default or {}).get("charges_enabled")
                if default
                else entry.get("stripe_charges_enabled")
            ),
            "accounts": pool,
            "default_account_id": (default or {}).get("id"),
        },
        "paypal": {
            "connected": bool(paypal_merchant or paypal_email),
            "paypal_merchant_id": paypal_merchant,
            "paypal_email": paypal_email,
            "connection_status": entry.get("paypal_connection_status"),
        },
        "nowpayments": {
            "connected": bool(now_key or now_hint),
            "api_key_hint": now_hint,
            "connection_status": entry.get("nowpayments_connection_status"),
        },
    }


@router.post("/stripe/connect/start")
def start_root_stripe_connect(
    payload: StripePoolConnectPayload,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, str]:
    accounts = _load_accounts_raw()
    _ensure_stripe_pool(accounts)
    frontend_url = resolve_frontend_url(payload.frontend_origin)
    view = payload.view
    name = (payload.name or "").strip() or None
    if view == "homepage" and not name:
        name = "Default"
    elif view == "popup" and not name:
        name = "Pop-up"
    return _start_view_or_pool_stripe_connect(
        accounts,
        view=view,
        frontend_url=frontend_url,
        name=name,
        account_entry_id=payload.account_entry_id,
    )


def _start_view_or_pool_stripe_connect(
    accounts: dict[str, Any],
    *,
    view: PaymentView | None,
    frontend_url: str,
    name: str | None,
    account_entry_id: str | None,
) -> dict[str, str]:
    pool = _ensure_stripe_pool(accounts)
    entry: dict[str, Any] | None = None

    if account_entry_id:
        entry = _find_pool_entry(accounts, account_entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Stripe account not found")
    elif view:
        view_acct = str((accounts.get(view) or {}).get("stripe_account_id") or "").strip()
        if view_acct:
            entry = next((e for e in pool if e.get("stripe_account_id") == view_acct), None)
        if not entry and view == "homepage":
            entry = next((e for e in pool if e.get("is_default")), None)

    if not entry:
        display_name = (name or "Stripe account").strip()[:120] or "Stripe account"
        entry = {
            "id": str(uuid.uuid4()),
            "name": display_name,
            "stripe_account_id": None,
            "connection_status": "pending",
            "charges_enabled": False,
            "is_default": len(pool) == 0,
        }
        pool.append(entry)
        accounts["stripe_accounts"] = pool
        _save_accounts(accounts)

    return_url = (
        f"{frontend_url}/super-admin/payment-accounts"
        f"?connected=1&provider=stripe&entry={entry['id']}"
    )
    refresh_url = (
        f"{frontend_url}/super-admin/payment-accounts"
        f"?refresh=1&provider=stripe&entry={entry['id']}"
    )

    if STRIPE_CONNECT_CLIENT_ID and use_stripe_standard_oauth():
        state = f"root:pool:{entry['id']}:{pack_origin_token(frontend_url)}"
        return {"url": build_stripe_oauth_authorize_url(state=state, frontend_url=frontend_url)}

    account_id = entry.get("stripe_account_id")
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
        entry["stripe_account_id"] = account_id
        entry["connection_status"] = "pending"
        entry["charges_enabled"] = False
        if view:
            accounts[view]["stripe_account_id"] = account_id
            accounts[view]["stripe_connection_status"] = "pending"
            accounts[view]["stripe_charges_enabled"] = False
        if entry.get("is_default"):
            _sync_default_stripe_to_views(accounts)
        _save_accounts(accounts)

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


@router.post("/stripe/rename")
def rename_root_stripe_account(
    payload: StripePoolRenamePayload,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    accounts = _load_accounts_raw()
    entry = _find_pool_entry(accounts, payload.account_entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Stripe account not found")
    entry["name"] = payload.name.strip()[:120]
    _save_accounts(accounts)
    return {"renamed": True, "stripe_accounts": _public_stripe_pool(accounts)}


@router.post("/stripe/set-default")
def set_default_root_stripe_account(
    payload: StripePoolEntryPayload,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    accounts = _load_accounts_raw()
    _set_pool_default(accounts, payload.account_entry_id)
    _save_accounts(accounts)
    return {"updated": True, "stripe_accounts": _public_stripe_pool(accounts)}


@router.post("/paypal/connect/start")
def start_root_paypal_connect(
    payload: PaymentViewPayload,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, str]:
    from routers.paypal_connect import get_paypal_connect_url

    view = payload.view
    state = f"root:{view}"
    try:
        url = get_paypal_connect_url(state, resolve_frontend_url(payload.frontend_origin))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Unable to start PayPal connect: {exc}",
        ) from exc
    return {"url": url}


def _apply_stripe_oauth_to_pool(
    accounts: dict[str, Any],
    *,
    entry_id: str | None,
    view: PaymentView | None,
    stripe_account_id: str,
    charges_enabled: bool,
    entry_name: str | None = None,
) -> str:
    """Attach a Connect account to a named platform pool entry.

    Always updates the target entry_id when provided — never deletes it.
    """
    pool = _ensure_stripe_pool(accounts)
    entry = _find_pool_entry(accounts, entry_id) if entry_id else None
    if not entry and view:
        view_acct = str((accounts.get(view) or {}).get("stripe_account_id") or "").strip()
        if view_acct:
            entry = next((e for e in pool if e.get("stripe_account_id") == view_acct), None)
        if not entry and view == "homepage":
            entry = next((e for e in pool if e.get("is_default")), None)

    status = "active" if charges_enabled else "pending"

    if not entry:
        entry = {
            "id": entry_id or str(uuid.uuid4()),
            "name": (
                (entry_name or "").strip()[:120]
                or (
                    "Default"
                    if view == "homepage" or not pool
                    else ("Pop-up" if view == "popup" else "Stripe account")
                )
            ),
            "stripe_account_id": stripe_account_id,
            "connection_status": status,
            "charges_enabled": charges_enabled,
            "is_default": len(pool) == 0,
        }
        pool.append(entry)
    else:
        if entry_name and not entry.get("name"):
            entry["name"] = entry_name.strip()[:120]
        entry["stripe_account_id"] = stripe_account_id
        entry["connection_status"] = status
        entry["charges_enabled"] = charges_enabled

    # If another pool row already pointed at this Connect account, clear the duplicate
    # pointer so each acct_ appears once — keep the entry the user just connected.
    for other in pool:
        if str(other.get("id")) == str(entry.get("id")):
            continue
        if other.get("stripe_account_id") == stripe_account_id:
            other["stripe_account_id"] = None
            other["connection_status"] = "pending"
            other["charges_enabled"] = False

    accounts["stripe_accounts"] = pool
    if view:
        accounts[view] = {
            **accounts[view],
            "stripe_account_id": stripe_account_id,
            "stripe_connection_status": status,
            "stripe_charges_enabled": charges_enabled,
        }
    if entry.get("is_default"):
        _sync_default_stripe_to_views(accounts)
    elif view == "homepage" and not any(e.get("is_default") for e in pool):
        entry["is_default"] = True
        _sync_default_stripe_to_views(accounts)
    _save_accounts(accounts)
    return str(entry["id"])


def handle_root_stripe_oauth_callback(code: str, state: str) -> RedirectResponse:
    if not state.startswith("root:"):
        raise HTTPException(status_code=400, detail="Invalid state")

    parts = state.split(":")
    kind = parts[1] if len(parts) > 1 else ""

    entry_id: str | None = None
    view: PaymentView | None = None
    frontend_origin = None

    if kind == "pool":
        entry_id = parts[2] if len(parts) > 2 else None
        frontend_origin = unpack_origin_token(parts[3]) if len(parts) > 3 else None
    elif kind in ("homepage", "popup"):
        view = kind  # type: ignore[assignment]
        frontend_origin = unpack_origin_token(parts[2]) if len(parts) > 2 else None
    else:
        raise HTTPException(status_code=400, detail="Invalid view")

    frontend_url = resolve_frontend_url(frontend_origin)

    def fail(message: str) -> RedirectResponse:
        return RedirectResponse(
            url=(
                f"{frontend_url}/super-admin/payment-accounts"
                f"?error={quote(message[:180], safe='')}&provider=stripe"
            )
        )

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
    except stripe.error.StripeError:
        # OAuth succeeded — persist the Connect id even if retrieve lags.
        charges_enabled = False

    accounts = _load_accounts_raw()
    try:
        saved_id = _apply_stripe_oauth_to_pool(
            accounts,
            entry_id=entry_id,
            view=view,
            stripe_account_id=stripe_account_id,
            charges_enabled=charges_enabled,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "Unable to save Stripe account"
        return fail(detail)
    except Exception as exc:
        return fail(f"Unable to save Stripe account: {exc}")

    return RedirectResponse(
        url=(
            f"{frontend_url}/super-admin/payment-accounts"
            f"?connected=1&provider=stripe&entry={saved_id}"
        )
    )


def save_root_paypal_account(state: str, merchant_id: str, email: str | None = None) -> str:
    if not state.startswith("root:"):
        raise HTTPException(status_code=400, detail="Invalid state")

    view = state.split(":", 1)[1]
    if view not in ("homepage", "popup"):
        raise HTTPException(status_code=400, detail="Invalid view")

    accounts = _load_accounts_raw()
    accounts[view] = {
        **accounts[view],
        "paypal_merchant_id": merchant_id,
        "paypal_email": email,
        "paypal_connection_status": "active",
    }
    _save_accounts(accounts)
    return f"/super-admin/payment-accounts?connected=1&provider=paypal&view={view}"


def handle_root_paypal_partner_callback(state: str, merchant_id: str) -> RedirectResponse:
    redirect_url = save_root_paypal_account(state, merchant_id)
    if redirect_url.startswith("/"):
        redirect_url = f"{resolve_frontend_url()}{redirect_url}"
    return RedirectResponse(url=redirect_url)


@router.post("/paypal/disconnect")
def disconnect_root_paypal(
    payload: PaymentViewPayload,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, bool]:
    accounts = _load_accounts_raw()
    view = payload.view
    accounts[view] = {
        **accounts[view],
        "paypal_merchant_id": None,
        "paypal_email": None,
        "paypal_connection_status": None,
    }
    _save_accounts(accounts)
    return {"removed": True}


@router.post("/stripe/disconnect")
def disconnect_root_stripe(
    payload: StripeDisconnectPayload,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, bool]:
    accounts = _load_accounts_raw()
    pool = _ensure_stripe_pool(accounts)

    if payload.account_entry_id:
        entry = _find_pool_entry(accounts, payload.account_entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Stripe account not found")
        was_default = bool(entry.get("is_default"))
        pool[:] = [e for e in pool if str(e.get("id")) != str(payload.account_entry_id)]
        if was_default and pool:
            pool[0]["is_default"] = True
        accounts["stripe_accounts"] = pool
        _sync_default_stripe_to_views(accounts)
        popup = accounts.get("popup") if isinstance(accounts.get("popup"), dict) else {}
        if popup.get("stripe_account_id") == entry.get("stripe_account_id"):
            accounts["popup"] = {
                **popup,
                "stripe_account_id": None,
                "stripe_connection_status": None,
                "stripe_charges_enabled": False,
            }
        _save_accounts(accounts)
        return {"removed": True}

    if payload.view not in ("homepage", "popup"):
        raise HTTPException(status_code=400, detail="account_entry_id or view is required")

    view = payload.view
    view_acct = (accounts[view] or {}).get("stripe_account_id")
    accounts[view] = {
        **accounts[view],
        "stripe_account_id": None,
        "stripe_connection_status": None,
        "stripe_charges_enabled": False,
    }
    if view_acct:
        remaining = [e for e in pool if e.get("stripe_account_id") != view_acct]
        if len(remaining) != len(pool):
            if remaining and not any(e.get("is_default") for e in remaining):
                remaining[0]["is_default"] = True
            accounts["stripe_accounts"] = remaining
            _sync_default_stripe_to_views(accounts)
    _save_accounts(accounts)
    return {"removed": True}


def handle_root_paypal_callback(code: str, state: str) -> RedirectResponse | None:
    if not state.startswith("root:"):
        return None

    from routers.paypal_connect import exchange_paypal_code

    view = state.split(":", 1)[1]
    if view not in ("homepage", "popup"):
        raise HTTPException(status_code=400, detail="Invalid view")

    merchant_id, email = exchange_paypal_code(code, f"{resolve_frontend_url()}/api/paypal/callback")
    redirect_url = save_root_paypal_account(state, merchant_id, email)
    if redirect_url.startswith("/"):
        redirect_url = f"{resolve_frontend_url()}{redirect_url}"
    return RedirectResponse(url=redirect_url)



def resolve_root_nowpayments_account(checkout_view: str | None) -> dict[str, Any] | None:
    view: PaymentView = "popup" if checkout_view == "popup" else "homepage"
    accounts = _load_accounts_raw()
    entry = accounts.get(view, {})
    api_key = entry.get("nowpayments_api_key")
    ipn_secret = entry.get("nowpayments_ipn_secret")
    if not api_key or not ipn_secret:
        return None
    status = entry.get("nowpayments_connection_status")
    if status and status not in ("active", "pending", "connected", None):
        return None
    return {
        "api_key": api_key,
        "ipn_secret": ipn_secret,
        "api_key_hint": entry.get("nowpayments_api_key_hint"),
        "connection_status": status or "active",
    }


class NowPaymentsAttachPayload(BaseModel):
    view: PaymentView
    api_key: str = Field(min_length=8, max_length=256)
    ipn_secret: str = Field(min_length=8, max_length=256)
    frontend_origin: str | None = None


@router.post("/nowpayments/attach")
def attach_root_nowpayments(
    payload: NowPaymentsAttachPayload,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, Any]:
    from nowpayments_client import api_key_hint, verify_api_key

    api_key = payload.api_key.strip()
    ipn_secret = payload.ipn_secret.strip()
    if not verify_api_key(api_key):
        raise HTTPException(
            status_code=400,
            detail="NOWPayments API key is invalid or the API is unreachable.",
        )

    accounts = _load_accounts_raw()
    view = payload.view
    accounts[view] = {
        **accounts[view],
        "nowpayments_api_key": api_key,
        "nowpayments_ipn_secret": ipn_secret,
        "nowpayments_api_key_hint": api_key_hint(api_key),
        "nowpayments_connection_status": "active",
    }
    _save_accounts(accounts)
    return {"attached": True, "view": view, "api_key_hint": api_key_hint(api_key)}


@router.post("/nowpayments/disconnect")
def disconnect_root_nowpayments(
    payload: PaymentViewPayload,
    user: Annotated[AuthUser, Depends(require_super_admin)],
) -> dict[str, bool]:
    accounts = _load_accounts_raw()
    view = payload.view
    accounts[view] = {
        **accounts[view],
        "nowpayments_api_key": None,
        "nowpayments_ipn_secret": None,
        "nowpayments_api_key_hint": None,
        "nowpayments_connection_status": None,
    }
    _save_accounts(accounts)
    return {"removed": True}
