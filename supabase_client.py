from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


def _supabase_url() -> str:
    return os.getenv("SUPABASE_URL", "").rstrip("/")


def _supabase_secret() -> str:
    return os.getenv("SUPABASE_SECRET_KEY", "")


def supabase_enabled() -> bool:
    return bool(_supabase_url() and _supabase_secret())


def _headers(*, prefer: str | None = None) -> dict[str, str]:
    secret = _supabase_secret()
    headers = {
        "apikey": secret,
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def insert_donation(row: dict[str, Any]) -> dict[str, Any] | None:
    if not supabase_enabled():
        return None

    try:
        response = httpx.post(
            f"{_supabase_url()}/rest/v1/donations",
            headers=_headers(prefer="return=representation,resolution=ignore-duplicates"),
            json=row,
            timeout=15.0,
        )
        if response.status_code in {200, 201}:
            data = response.json()
            return data[0] if isinstance(data, list) and data else data
        if response.status_code in {404, 409}:
            return None
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    return None


def list_donations(*, limit: int, offset: int) -> list[dict[str, Any]]:
    if not supabase_enabled():
        return []

    try:
        response = httpx.get(
            f"{_supabase_url()}/rest/v1/donations",
            headers=_headers(),
            params={
                "select": "id,first_name,last_name,amount,currency,frequency,honoree_name,created_at",
                "order": "created_at.desc",
                "limit": str(limit),
                "offset": str(offset),
            },
            timeout=15.0,
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else []
    except httpx.HTTPError:
        return []


def get_donation_by_payment_intent(payment_intent_id: str) -> dict[str, Any] | None:
    if not supabase_enabled():
        return None

    try:
        response = httpx.get(
            f"{_supabase_url()}/rest/v1/donations",
            headers=_headers(),
            params={
                "select": "id,first_name,last_name,amount,currency,frequency,honoree_name,created_at,stripe_payment_intent_id",
                "stripe_payment_intent_id": f"eq.{payment_intent_id}",
                "limit": "1",
            },
            timeout=15.0,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list) and data:
            return data[0]
    except httpx.HTTPError:
        return None
    return None
