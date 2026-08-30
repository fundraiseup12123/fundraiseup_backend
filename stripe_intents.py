from __future__ import annotations

from typing import Any

import stripe

from db import rest_get


def stripe_request_kwargs(stripe_account: str | None) -> dict[str, str]:
    if stripe_account:
        return {"stripe_account": stripe_account}
    return {}


def _list_connect_account_ids() -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        account_id = (value or "").strip()
        if account_id and account_id not in seen:
            seen.add(account_id)
            ids.append(account_id)

    try:
        for row in rest_get(
            "stripe_accounts",
            params={"select": "stripe_account_id", "limit": "200"},
        ):
            add(row.get("stripe_account_id"))
    except Exception:
        pass

    try:
        from routers.payment_accounts import _load_accounts_raw

        for view in _load_accounts_raw().values():
            if isinstance(view, dict):
                add(view.get("stripe_account_id"))
            elif isinstance(view, list):
                for entry in view:
                    if isinstance(entry, dict):
                        add(entry.get("stripe_account_id"))
    except Exception:
        pass

    return ids


def _resolve_pi_from_sub_or_invoice(
    identifier: str,
    *,
    stripe_account: str | None = None,
    expand: list[str] | None = None,
) -> tuple[stripe.PaymentIntent, str | None] | None:
    expand_opts = {"expand": expand} if expand else {}
    acct_kwargs = stripe_request_kwargs(stripe_account)

    # 1) Subscription ID (sub_...)
    if identifier.startswith("sub_"):
        try:
            sub = stripe.Subscription.retrieve(identifier, **acct_kwargs)
            inv_id = (
                sub.latest_invoice
                if isinstance(sub.latest_invoice, str)
                else getattr(sub.latest_invoice, "id", None)
            )
            if inv_id:
                return _resolve_pi_from_sub_or_invoice(
                    inv_id, stripe_account=stripe_account, expand=expand
                )
        except Exception:
            return None

    # 2) Invoice ID (in_...)
    if identifier.startswith("in_"):
        try:
            inv = stripe.Invoice.retrieve(
                identifier,
                expand=["payments.data.payment.payment_intent", "payment_intent"],
                **acct_kwargs,
            )
            pi = getattr(inv, "payment_intent", None)
            if pi:
                if isinstance(pi, str):
                    return (
                        stripe.PaymentIntent.retrieve(
                            pi, stripe_account=stripe_account, **expand_opts
                        ),
                        stripe_account,
                    )
                return pi, stripe_account

            payments = getattr(inv, "payments", None)
            if payments and getattr(payments, "data", None):
                for p in payments.data:
                    pay_obj = getattr(p, "payment", None)
                    if pay_obj:
                        pi_obj = getattr(pay_obj, "payment_intent", None)
                        if pi_obj:
                            if isinstance(pi_obj, str):
                                return (
                                    stripe.PaymentIntent.retrieve(
                                        pi_obj,
                                        stripe_account=stripe_account,
                                        **expand_opts,
                                    ),
                                    stripe_account,
                                )
                            return pi_obj, stripe_account
        except Exception:
            return None

    return None


def retrieve_payment_intent(
    payment_intent_id: str,
    *,
    stripe_account: str | None = None,
    expand: list[str] | None = None,
) -> tuple[stripe.PaymentIntent, str | None]:
    """Retrieve a payment intent from the platform or a connected account.
    Supports PaymentIntent IDs (pi_...), Subscription IDs (sub_...), and Invoice IDs (in_...).
    """
    identifier = (payment_intent_id or "").strip()
    expand_opts = {"expand": expand} if expand else {}

    # Check if this identifier is a subscription or invoice first
    if identifier.startswith("sub_") or identifier.startswith("in_"):
        if stripe_account:
            res = _resolve_pi_from_sub_or_invoice(
                identifier, stripe_account=stripe_account, expand=expand
            )
            if res:
                return res
        else:
            # Try platform
            res = _resolve_pi_from_sub_or_invoice(identifier, stripe_account=None, expand=expand)
            if res:
                return res
            # Try connected accounts
            for account_id in _list_connect_account_ids():
                res = _resolve_pi_from_sub_or_invoice(
                    identifier, stripe_account=account_id, expand=expand
                )
                if res:
                    return res

    if stripe_account:
        return (
            stripe.PaymentIntent.retrieve(
                identifier,
                stripe_account=stripe_account,
                **expand_opts,
            ),
            stripe_account,
        )

    try:
        return stripe.PaymentIntent.retrieve(identifier, **expand_opts), None
    except stripe.error.InvalidRequestError as exc:
        message = str(exc.user_message or exc).lower()
        if "no such payment_intent" not in message and "no such paymentintent" not in message:
            raise

    for account_id in _list_connect_account_ids():
        try:
            return (
                stripe.PaymentIntent.retrieve(
                    identifier,
                    stripe_account=account_id,
                    **expand_opts,
                ),
                account_id,
            )
        except stripe.error.InvalidRequestError:
            continue

    raise stripe.error.InvalidRequestError(
        message=f"No such payment_intent: '{identifier}'",
        param="intent",
    )
