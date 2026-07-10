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

    for row in rest_get(
        "stripe_accounts",
        params={"select": "stripe_account_id", "limit": "200"},
    ):
        add(row.get("stripe_account_id"))

    try:
        from routers.payment_accounts import _load_accounts_raw

        for view in _load_accounts_raw().values():
            if isinstance(view, dict):
                add(view.get("stripe_account_id"))
    except Exception:
        pass

    return ids


def retrieve_payment_intent(
    payment_intent_id: str,
    *,
    stripe_account: str | None = None,
) -> tuple[stripe.PaymentIntent, str | None]:
    """Retrieve a payment intent from the platform or a connected account."""
    if stripe_account:
        return (
            stripe.PaymentIntent.retrieve(
                payment_intent_id,
                stripe_account=stripe_account,
            ),
            stripe_account,
        )

    try:
        return stripe.PaymentIntent.retrieve(payment_intent_id), None
    except stripe.error.InvalidRequestError as exc:
        message = str(exc.user_message or exc).lower()
        if "no such payment_intent" not in message and "no such paymentintent" not in message:
            raise

    for account_id in _list_connect_account_ids():
        try:
            return (
                stripe.PaymentIntent.retrieve(
                    payment_intent_id,
                    stripe_account=account_id,
                ),
                account_id,
            )
        except stripe.error.InvalidRequestError:
            continue

    raise stripe.error.InvalidRequestError(
        message=f"No such payment_intent: '{payment_intent_id}'",
        param="intent",
    )
