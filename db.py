from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote

import httpx
from env_loader import load_app_env

load_app_env()

logger = logging.getLogger(__name__)


def supabase_url() -> str:
    return os.getenv("SUPABASE_URL", "").rstrip("/")


def supabase_secret() -> str:
    return os.getenv("SUPABASE_SECRET_KEY", "")


def supabase_enabled() -> bool:
    return bool(supabase_url() and supabase_secret())


def _headers(*, prefer: str | None = None, user_jwt: str | None = None) -> dict[str, str]:
    secret = supabase_secret()
    token = user_jwt or secret
    headers = {
        "apikey": secret,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def rest_get(
    table: str,
    *,
    params: dict[str, str] | None = None,
    user_jwt: str | None = None,
) -> list[dict[str, Any]]:
    if not supabase_enabled():
        return []

    req_params = dict(params or {})
    raw_limit_str = req_params.get("limit")

    target_limit = 100000
    if raw_limit_str:
        try:
            target_limit = int(raw_limit_str)
        except ValueError:
            target_limit = 100000

    if target_limit <= 1000:
        try:
            response = httpx.get(
                f"{supabase_url()}/rest/v1/{table}",
                headers=_headers(user_jwt=user_jwt),
                params=req_params,
                timeout=20.0,
            )
            if response.status_code >= 400:
                return []
            data = response.json()
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("Supabase request failed for %s: %s", table, exc)
            return []

    all_rows: list[dict[str, Any]] = []
    base_offset = 0
    if "offset" in req_params:
        try:
            base_offset = int(req_params["offset"])
        except ValueError:
            base_offset = 0

    chunk_size = 1000
    current_offset = base_offset

    while len(all_rows) < target_limit:
        chunk_limit = min(chunk_size, target_limit - len(all_rows))
        chunk_params = {**req_params, "limit": str(chunk_limit), "offset": str(current_offset)}
        try:
            response = httpx.get(
                f"{supabase_url()}/rest/v1/{table}",
                headers=_headers(user_jwt=user_jwt),
                params=chunk_params,
                timeout=20.0,
            )
            if response.status_code >= 400:
                break
            data = response.json()
            if not isinstance(data, list) or not data:
                break
            all_rows.extend(data)
            if len(data) < chunk_limit:
                break
            current_offset += len(data)
        except Exception as exc:
            logger.warning("Supabase paginated request failed for %s at offset %s: %s", table, current_offset, exc)
            break

    return all_rows


def rest_get_one(
    table: str,
    *,
    params: dict[str, str],
    user_jwt: str | None = None,
) -> dict[str, Any] | None:
    rows = rest_get(table, params={**params, "limit": "1"}, user_jwt=user_jwt)
    return rows[0] if rows else None


def rest_insert(
    table: str,
    row: dict[str, Any] | list[dict[str, Any]],
    *,
    user_jwt: str | None = None,
    on_conflict: str | None = None,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    result, _error = rest_insert_result(table, row, user_jwt=user_jwt, on_conflict=on_conflict)
    return result


def rest_upsert(
    table: str,
    row: dict[str, Any] | list[dict[str, Any]],
    *,
    on_conflict: str,
    user_jwt: str | None = None,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    return rest_insert(table, row, user_jwt=user_jwt, on_conflict=on_conflict)


def rest_insert_result(
    table: str,
    row: dict[str, Any] | list[dict[str, Any]],
    *,
    user_jwt: str | None = None,
    on_conflict: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Insert/upsert a row. Returns (row, error_message)."""
    if not supabase_enabled():
        return None, "Supabase is not configured"
    prefer = "return=representation"
    if on_conflict:
        prefer = f"resolution=merge-duplicates,{prefer}"
    params: dict[str, str] = {}
    if on_conflict:
        params["on_conflict"] = on_conflict
    try:
        response = httpx.post(
            f"{supabase_url()}/rest/v1/{table}",
            headers=_headers(prefer=prefer, user_jwt=user_jwt),
            params=params or None,
            json=row,
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        logger.warning("Supabase insert failed for %s: %s", table, exc)
        return None, str(exc)
    if response.status_code not in {200, 201}:
        logger.warning(
            "Supabase insert failed for %s (%s): %s",
            table,
            response.status_code,
            response.text[:500],
        )
        try:
            body = response.json()
            if isinstance(body, dict):
                message = body.get("message") or body.get("hint") or body.get("details") or response.text
                return None, str(message)[:300]
        except Exception:
            pass
        return None, (response.text or f"Insert failed ({response.status_code})")[:300]
    data = response.json()
    if isinstance(data, list):
        if not data:
            return None, "Insert returned no rows"
        return data[0] if isinstance(data[0], dict) else None, None
    if isinstance(data, dict):
        return data, None
    return None, "Unexpected insert response"


def rest_patch_result(
    table: str,
    row: dict[str, Any],
    *,
    match: dict[str, str],
    user_jwt: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not supabase_enabled():
        return None, "Supabase is not configured"
    params = {k: f"eq.{v}" for k, v in match.items()}
    try:
        response = httpx.patch(
            f"{supabase_url()}/rest/v1/{table}",
            headers=_headers(prefer="return=representation", user_jwt=user_jwt),
            params=params,
            json=row,
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        logger.warning("Supabase patch failed for %s: %s", table, exc)
        return None, str(exc)
    if response.status_code not in {200, 204}:
        logger.warning(
            "Supabase patch failed for %s (%s): %s",
            table,
            response.status_code,
            response.text[:500],
        )
        return None, (response.text or f"Patch failed ({response.status_code})")[:300]
    if response.status_code == 204 or not response.content:
        return {"id": match.get("id")}, None
    data = response.json()
    if isinstance(data, list):
        if not data:
            return None, "Patch matched no rows"
        return data[0] if isinstance(data[0], dict) else None, None
    if isinstance(data, dict):
        return data, None
    return None, "Unexpected patch response"


def rest_insert_error(
    table: str,
    row: dict[str, Any] | list[dict[str, Any]],
    *,
    user_jwt: str | None = None,
) -> str | None:
    """Return error message from Supabase if insert failed."""
    if not supabase_enabled():
        return "Supabase is not configured"
    try:
        response = httpx.post(
            f"{supabase_url()}/rest/v1/{table}",
            headers=_headers(prefer="return=representation", user_jwt=user_jwt),
            json=row,
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        return str(exc)
    if response.status_code in {200, 201}:
        return None
    try:
        body = response.json()
        if isinstance(body, dict):
            return body.get("message") or body.get("hint") or body.get("details") or response.text
    except Exception:
        pass
    return response.text or f"Insert failed ({response.status_code})"


def rest_patch(
    table: str,
    row: dict[str, Any],
    *,
    match: dict[str, str],
    user_jwt: str | None = None,
) -> dict[str, Any] | None:
    if not supabase_enabled():
        return None
    params = {k: f"eq.{v}" for k, v in match.items()}
    try:
        response = httpx.patch(
            f"{supabase_url()}/rest/v1/{table}",
            headers=_headers(prefer="return=representation", user_jwt=user_jwt),
            params=params,
            json=row,
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        logger.warning("Supabase patch failed for %s: %s", table, exc)
        return None
    if response.status_code not in {200, 204}:
        return None
    data = response.json()
    if isinstance(data, list) and data:
        return data[0]
    return data if isinstance(data, dict) else None


def rest_delete(
    table: str,
    *,
    match: dict[str, str],
    user_jwt: str | None = None,
) -> bool:
    if not supabase_enabled():
        return False
    params = {k: f"eq.{v}" for k, v in match.items()}
    try:
        response = httpx.delete(
            f"{supabase_url()}/rest/v1/{table}",
            headers=_headers(user_jwt=user_jwt),
            params=params,
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        logger.warning("Supabase delete failed for %s: %s", table, exc)
        return False
    return response.status_code in {200, 204}


def eq(column: str, value: str) -> str:
    return f"eq.{value}"


def select_columns(*cols: str) -> str:
    return ",".join(cols)
