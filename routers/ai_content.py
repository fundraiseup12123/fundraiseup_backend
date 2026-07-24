from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from html import escape
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import AuthUser, has_global_org_access, require_auth, require_org_access
from db import rest_get, rest_get_one

router = APIRouter(prefix="/admin", tags=["ai-content"])

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
# Cheapest solid OpenAI chat model (override with OPENAI_MODEL).
DEFAULT_OPENAI_MODEL = "gpt-4.1-nano"
ROOT_TARGET = 1700
ROOT_MAX = 1750
POPUP_TARGET = 375
POPUP_MAX = 400
POPUP_EXPAND_BELOW = 320


class CampaignContentAiRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    campaign_title: str | None = Field(default=None, max_length=500)
    current_desktop_body: str | None = Field(default=None, max_length=20000)
    current_mobile_body: str | None = Field(default=None, max_length=20000)
    current_popup_body: str | None = Field(default=None, max_length=8000)
    current_popup_body_mobile: str | None = Field(default=None, max_length=8000)


def _plain_len(text: str) -> int:
    no_md = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "")
    no_md = re.sub(r"__(.+?)__", r"\1", no_md)
    return len(no_md.strip())


def _soft_fit_multiline(text: str, max_chars: int) -> str:
    cleaned = (text or "").replace("\r\n", "\n").strip()
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if _plain_len(cleaned) <= max_chars:
        return cleaned

    # Prefer ending on a full paragraph, then sentence, then word.
    cut = cleaned[:max_chars]
    para = cut.rfind("\n\n")
    if para >= int(max_chars * 0.55):
        return cut[:para].strip()
    for sep in (". ", "! ", "? ", ".\n", "!\n", "?\n", "\n", " "):
        idx = cut.rfind(sep)
        if idx >= int(max_chars * 0.55):
            end = idx + (1 if sep.strip() and not sep.endswith("\n") else len(sep.rstrip("\n")) )
            # Keep trailing punctuation when sep is ". "
            if sep in {". ", "! ", "? "}:
                return cut[: idx + 1].strip()
            if sep.endswith("\n"):
                return cut[:idx].strip()
            return cut[:end].strip()
    return cut.rstrip()


def _apply_bold(escaped: str) -> str:
    with_bold = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    return re.sub(r"__(.+?)__", r"<strong>\1</strong>", with_bold)


def _markdown_bold_to_html(text: str) -> str:
    cleaned = (text or "").replace("\r\n", "\n").strip()
    if not cleaned:
        return ""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", cleaned) if block.strip()]
    paragraphs: list[str] = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        joined = "<br />".join(_apply_bold(escape(line)) for line in lines)
        paragraphs.append(f"<p>{joined}</p>")
    return "".join(paragraphs)


def _fit_marked_text(text: str, max_chars: int) -> str:
    cleaned = (text or "").replace("\r\n", "\n").strip()
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if _plain_len(cleaned) <= max_chars:
        return cleaned
    plain = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    plain = re.sub(r"__(.+?)__", r"\1", plain)
    return _soft_fit_multiline(plain, max_chars)


def _extract_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("Empty model response")
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError("Model did not return JSON")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("Model JSON was not an object")
    return data


def _build_messages(payload: CampaignContentAiRequest) -> list[dict[str, str]]:
    title = (payload.campaign_title or "").strip() or "this campaign"
    system = (
        "You write fundraising campaign copy for 4 placements. "
        "Return ONLY valid JSON with keys: "
        "desktop_body, mobile_body, popup_body, popup_body_mobile. "
        "Use plain text with real line breaks and blank lines between paragraphs. "
        "Use **double asterisks** for bold on key phrases, amounts, and section labels. "
        "Include relevant emojis where they help emotion and scanning "
        "(examples: 💔 👇 🇵🇸 ❤️). "
        "Handle special characters normally (quotes, apostrophes, currency symbols like $). "
        "Do not use HTML tags. Do not use markdown headings (#) or bullet markers like - or * for lists; "
        "use emoji line starters or short labeled lines instead.\n\n"
        f"desktop_body (root desktop): about {ROOT_TARGET} characters (soft max {ROOT_MAX}). "
        "Long emotional landing-page story with spacing, bold highlights, emoji pain points, "
        "and optional donation amount lines if it fits the prompt.\n"
        f"mobile_body (root mobile): about {ROOT_TARGET} characters (soft max {ROOT_MAX}). "
        "Same story quality as desktop, but slightly tighter paragraphs for phones.\n"
        f"popup_body (popup desktop): REQUIRED length about {POPUP_TARGET} characters "
        f"(never under ~350, soft max {POPUP_MAX}). "
        "NEVER return a single short slogan or one sentence. "
        "A one-liner like 'Baby needs milk. Donate $10.' is INVALID. "
        "Must be a full mini-story with blank lines:\n"
        "1) First line = short urgent headline with emoji "
        "(example: Gaza is Dying: Save a Life Before It’s Too 💔)\n"
        "2) Blank line\n"
        "3) Full paragraph (~2 sentences) about daily struggle\n"
        "4) Blank line\n"
        "5) Full paragraph (~2 sentences) about how support helps + emotional close\n"
        "Use **bold** on a few key phrases. Rewrite to fit fully; never end mid-sentence.\n"
        f"popup_body_mobile: same full structure and length (~{POPUP_TARGET}, max {POPUP_MAX}).\n"
        "Count characters as the final readable text (including spaces and emojis)."
    )
    parts = [
        f"Campaign title: {title}",
        f"User prompt: {payload.prompt.strip()}",
        "Style reference for ROOT (desktop/mobile) structure:\n"
        '- Optional faith/quote line in quotes\n'
        "- Emoji lines for urgent needs\n"
        "- Emotional paragraphs with blank lines between them\n"
        "- Optional donation tiers with bold amounts\n"
        "- Strong closing CTA with emoji\n"
        "Style reference for POPUP (match this shape and length):\n"
        "Gaza is Dying: Save a Life Before It’s Too 💔\n\n"
        "In Gaza, countless children face hunger, displacement, and uncertainty every day. "
        "Many families are struggling to access clean water, hot meals, and fresh bread.\n\n"
        "Through Hope for Gaza, your support helps deliver life-saving aid to those who need it most. "
        "Stand with Gaza's children—the most innocent and vulnerable victims of this crisis. "
        "Your donation can bring hope, relief, and a chance for a better tomorrow.",
    ]
    if (payload.current_desktop_body or "").strip():
        parts.append(f"Current root desktop body:\n{payload.current_desktop_body.strip()}")
    if (payload.current_mobile_body or "").strip():
        parts.append(f"Current root mobile body:\n{payload.current_mobile_body.strip()}")
    if (payload.current_popup_body or "").strip():
        parts.append(f"Current popup desktop body:\n{payload.current_popup_body.strip()}")
    if (payload.current_popup_body_mobile or "").strip():
        parts.append(f"Current popup mobile body:\n{payload.current_popup_body_mobile.strip()}")
    if any(
        [
            (payload.current_desktop_body or "").strip(),
            (payload.current_mobile_body or "").strip(),
            (payload.current_popup_body or "").strip(),
            (payload.current_popup_body_mobile or "").strip(),
        ]
    ):
        parts.append(
            "Revise the current copy using the user prompt. Keep what still works; improve the rest."
        )
    parts.append(
        "Respond with JSON like: "
        '{"desktop_body":"...","mobile_body":"...","popup_body":"...","popup_body_mobile":"..."}'
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def _openai_chat(
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 1200,
    temperature: float = 0.45,
    json_mode: bool = True,
) -> str:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        with httpx.Client(timeout=90.0) as client:
            response = client.post(
                OPENAI_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="Cannot reach OpenAI. Check your internet/DNS and try again.",
        ) from exc

    if response.status_code >= 400:
        detail = "OpenAI API error"
        try:
            err = response.json()
            detail = str((err.get("error") or {}).get("message") or err.get("message") or detail)
        except Exception:
            detail = response.text[:300] or detail
        raise HTTPException(status_code=502, detail=detail)

    try:
        data = response.json()
        return str((((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or "")
    except Exception as exc:
        raise HTTPException(status_code=502, detail="OpenAI returned unreadable content") from exc


def _openai_json(
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 1200,
    temperature: float = 0.45,
) -> dict[str, Any]:
    raw = _openai_chat(
        api_key,
        model,
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        json_mode=True,
    )
    try:
        return _extract_json(raw)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="AI returned unreadable content") from exc


def _expand_short_popup(
    *,
    api_key: str,
    model: str,
    campaign_title: str,
    prompt: str,
    draft: str,
) -> str:
    """If the model returned a slogan, rewrite into full ~350-400 popup copy."""
    if _plain_len(draft) >= POPUP_EXPAND_BELOW:
        return draft

    messages = [
        {
            "role": "system",
            "content": (
                "Expand short fundraising popup copy into a full modal body. "
                "Return ONLY JSON: {\"popup_body\":\"...\"}. "
                f"Target about {POPUP_TARGET} characters, soft max {POPUP_MAX}. "
                "Required shape with blank lines:\n"
                "headline with emoji\n\n"
                "struggle paragraph (2 sentences)\n\n"
                "impact/help paragraph (2 sentences)\n"
                "Use **bold** on a few key words. Include 1-2 emojis. No HTML."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Campaign title: {campaign_title}\n"
                f"User prompt: {prompt}\n"
                f"Too-short draft:\n{draft or '(empty)'}\n\n"
                "Example length/shape to match:\n"
                "Gaza is Dying: Save a Life Before It’s Too 💔\n\n"
                "In Gaza, countless children face hunger, displacement, and uncertainty every day. "
                "Many families are struggling to access clean water, hot meals, and fresh bread.\n\n"
                "Through Hope for Gaza, your support helps deliver life-saving aid to those who need it most. "
                "Stand with Gaza's children—the most innocent and vulnerable victims of this crisis. "
                "Your donation can bring hope, relief, and a chance for a better tomorrow."
            ),
        },
    ]
    parsed = _openai_json(api_key, model, messages, max_tokens=800)
    expanded = str(parsed.get("popup_body") or "").strip()
    return expanded or draft


@router.post("/orgs/{org_id}/ai/campaign-content")
def generate_campaign_content(
    org_id: str,
    payload: CampaignContentAiRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, str]:
    require_org_access(org_id, user, min_role="admin")

    api_key, model = _require_openai()
    parsed = _openai_json(api_key, model, _build_messages(payload), max_tokens=4500)

    desktop = _fit_marked_text(str(parsed.get("desktop_body") or ""), ROOT_MAX)
    mobile = _fit_marked_text(str(parsed.get("mobile_body") or ""), ROOT_MAX)
    popup = _fit_marked_text(str(parsed.get("popup_body") or ""), POPUP_MAX)
    popup_mobile = _fit_marked_text(
        str(parsed.get("popup_body_mobile") or parsed.get("popup_body") or ""),
        POPUP_MAX,
    )

    title = (payload.campaign_title or "").strip() or "this campaign"
    prompt = payload.prompt.strip()
    popup = _fit_marked_text(
        _expand_short_popup(
            api_key=api_key,
            model=model,
            campaign_title=title,
            prompt=prompt,
            draft=popup,
        ),
        POPUP_MAX,
    )
    popup_mobile = _fit_marked_text(
        _expand_short_popup(
            api_key=api_key,
            model=model,
            campaign_title=title,
            prompt=prompt,
            draft=popup_mobile or popup,
        ),
        POPUP_MAX,
    )

    if not desktop and not mobile and not popup and not popup_mobile:
        raise HTTPException(status_code=502, detail="AI returned empty content")

    return {
        "desktop_body": _markdown_bold_to_html(desktop),
        "mobile_body": _markdown_bold_to_html(mobile or desktop),
        "popup_body": _markdown_bold_to_html(popup or desktop),
        "popup_body_mobile": _markdown_bold_to_html(popup_mobile or popup or desktop),
    }


# --- AI Features (Helper / Analytics / Org Dashboard) ---


class AbHelperRequest(BaseModel):
    campaign_id: str = Field(min_length=1, max_length=80)


class AnalyticsExplainRequest(BaseModel):
    campaign_id: str | None = Field(default=None, max_length=80)
    range: str = Field(default="daily", max_length=20)


class OrgDashboardRequest(BaseModel):
    pass


class InsightsChatMessage(BaseModel):
    role: str = Field(min_length=1, max_length=20)
    content: str = Field(min_length=1, max_length=4000)


class InsightsChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    campaign_id: str | None = Field(default=None, max_length=80)
    history: list[InsightsChatMessage] = Field(default_factory=list, max_length=20)
    include_cross_check: bool = False


class TranslateTexts(BaseModel):
    title: str = ""
    titleHtml: str = ""
    titleHtmlMobile: str = ""
    caption: str = ""
    captionMobile: str = ""
    bodyHtml: str = ""
    bodyHtmlMobile: str = ""
    dedicationHint: str = ""
    landingHeadlineHtml: str = ""
    landingBodyHtml: str = ""
    modalTitle: str = ""
    modalTitleHtml: str = ""
    modalBodyHtml: str = ""
    modalTitleMobile: str = ""
    modalTitleHtmlMobile: str = ""
    modalBodyHtmlMobile: str = ""


class TranslateCampaignRequest(BaseModel):
    campaign_id: str = Field(min_length=1, max_length=80)
    target_language: str = Field(min_length=2, max_length=16)
    texts: TranslateTexts


def _require_openai() -> tuple[str, str]:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="AI is not configured (missing OPENAI_API_KEY)")
    model = (os.getenv("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL).strip() or DEFAULT_OPENAI_MODEL
    return api_key, model


def _campaign_bundle(org_id: str, campaign_id: str) -> dict[str, Any]:
    cid = (campaign_id or "").strip()
    oid = (org_id or "").strip()
    if not cid or not oid:
        raise HTTPException(status_code=400, detail="Campaign and organization are required")

    # Keep select columns conservative — unknown columns make PostgREST 400 and look like "not found".
    campaign = rest_get_one(
        "campaigns",
        params={
            "id": f"eq.{cid}",
            "organization_id": f"eq.{oid}",
            "select": "id,name,slug,status,default_currency,primary_color",
        },
    )
    if not campaign:
        # Retry without primary_color for schemas that omit branding columns.
        campaign = rest_get_one(
            "campaigns",
            params={
                "id": f"eq.{cid}",
                "organization_id": f"eq.{oid}",
                "select": "id,name,slug,status,default_currency",
            },
        )
    if not campaign:
        # Last resort: id only, then verify org (helps diagnose mismatches).
        any_org = rest_get_one(
            "campaigns",
            params={"id": f"eq.{cid}", "select": "id,name,slug,status,default_currency,organization_id"},
        )
        if any_org and str(any_org.get("organization_id") or "") != oid:
            raise HTTPException(
                status_code=404,
                detail="That campaign belongs to a different organization. Re-select the campaign and try again.",
            )
        if not any_org:
            raise HTTPException(
                status_code=404,
                detail="Campaign not found. Refresh the page and pick the campaign again.",
            )
        campaign = any_org

    content = rest_get_one(
        "campaign_content",
        params={"campaign_id": f"eq.{cid}", "select": "*"},
    ) or {}
    return {"campaign": campaign, "content": content}


def _device_type(device: Any) -> str:
    if isinstance(device, dict):
        return str(device.get("type") or device.get("Type") or "").strip().lower()
    return str(device or "").strip().lower()


def _is_mobile_device(device: Any) -> bool:
    t = _device_type(device)
    return t in {"mobile", "tablet"} or "mobile" in t


def _countable_donations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    excluded = {"failed", "canceled", "cancelled", "refunded", "disputed"}
    return [r for r in rows if str(r.get("status") or "").lower() not in excluded]


def _campaign_amount_presets(campaign_id: str) -> list[dict[str, Any]]:
    rows = rest_get(
        "campaign_currencies",
        params={
            "campaign_id": f"eq.{campaign_id}",
            "select": "currency_code,amounts_once,amounts_monthly,is_default",
            "order": "is_default.desc",
        },
    ) or []
    out: list[dict[str, Any]] = []
    for row in rows[:4]:
        out.append(
            {
                "currency": row.get("currency_code"),
                "is_default": bool(row.get("is_default")),
                "once": row.get("amounts_once") or [],
                "monthly": row.get("amounts_monthly") or [],
            }
        )
    return out


def _campaign_performance(org_id: str, campaign_id: str) -> dict[str, Any]:
    rows = rest_get(
        "donations",
        params={
            "organization_id": f"eq.{org_id}",
            "campaign_id": f"eq.{campaign_id}",
            "select": "amount,currency,frequency,created_at,status,device,payment_method,utm",
            "order": "created_at.desc",
            "limit": "300",
        },
    ) or []
    ok = _countable_donations(rows)
    now = datetime.now(timezone.utc)
    def _parse_dt(value: Any) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None

    last_7 = 0
    last_30 = 0
    amounts: list[float] = []
    monthly = 0
    mobile = 0
    methods: dict[str, int] = {}
    utm_sources: dict[str, int] = {}
    for r in ok:
        amt = float(r.get("amount") or 0)
        amounts.append(amt)
        if r.get("frequency") == "monthly":
            monthly += 1
        if _is_mobile_device(r.get("device")):
            mobile += 1
        method = str(r.get("payment_method") or "unknown")
        methods[method] = methods.get(method, 0) + 1
        utm = r.get("utm") if isinstance(r.get("utm"), dict) else {}
        src = str((utm or {}).get("source") or "").strip()
        if src:
            utm_sources[src] = utm_sources.get(src, 0) + 1
        created = _parse_dt(r.get("created_at"))
        if created:
            age = (now - created.astimezone(timezone.utc)).total_seconds()
            if age <= 7 * 86400:
                last_7 += 1
            if age <= 30 * 86400:
                last_30 += 1

    total = sum(amounts)
    return {
        "donation_count": len(ok),
        "total_amount": round(total, 2),
        "avg_gift": round(total / len(amounts), 2) if amounts else 0,
        "monthly_count": monthly,
        "once_count": max(0, len(ok) - monthly),
        "mobile_share_pct": round((mobile / len(ok) * 100) if ok else 0, 1),
        "gifts_last_7_days": last_7,
        "gifts_last_30_days": last_30,
        "payment_methods": methods,
        "top_utm_sources": sorted(
            [{"source": k, "count": v} for k, v in utm_sources.items()],
            key=lambda x: x["count"],
            reverse=True,
        )[:5],
        "sample_size": len(rows),
    }


def _optional_ga4_snapshot(org_id: str, campaign_id: str | None = None) -> dict[str, Any] | None:
    """Best-effort GA4 context — never required for AI Features to run."""
    try:
        from ga4_client import fetch_realtime_snapshot, ga4_configured, get_property_id
    except Exception:
        return None
    if not ga4_configured():
        return None

    property_id = get_property_id()
    if campaign_id:
        content = rest_get_one(
            "campaign_content",
            params={"campaign_id": f"eq.{campaign_id}", "select": "ga4_property_id"},
        ) or {}
        prop = str(content.get("ga4_property_id") or "").replace("properties/", "").strip()
        if prop:
            property_id = prop
    if not property_id:
        return None
    try:
        snap = fetch_realtime_snapshot(property_id=property_id)
        if not isinstance(snap, dict):
            return None
        return {"property_id": property_id, "realtime": snap}
    except Exception:
        return None


def _org_donation_snapshot(org_id: str, campaign_id: str | None = None) -> dict[str, Any]:
    campaigns = rest_get(
        "campaigns",
        params={"organization_id": f"eq.{org_id}", "select": "id,name,status"},
    ) or []
    params: dict[str, str] = {
        "organization_id": f"eq.{org_id}",
        "select": "amount,currency,frequency,created_at,campaign_id,status,device,payment_method,utm",
        "order": "created_at.desc",
        "limit": "400",
    }
    if campaign_id:
        params["campaign_id"] = f"eq.{campaign_id}"
    rows = rest_get("donations", params=params) or []
    ok = _countable_donations(rows)
    total = sum(float(r.get("amount") or 0) for r in ok)
    monthly = sum(1 for r in ok if r.get("frequency") == "monthly")
    mobile = sum(1 for r in ok if _is_mobile_device(r.get("device")))
    by_campaign: dict[str, float] = {}
    by_campaign_count: dict[str, int] = {}
    name_by_id = {c["id"]: c.get("name") or "Campaign" for c in campaigns}
    status_by_id = {c["id"]: c.get("status") or "" for c in campaigns}
    methods: dict[str, int] = {}
    now = datetime.now(timezone.utc)
    last_7 = 0
    last_30 = 0
    for r in ok:
        cid = r.get("campaign_id") or ""
        by_campaign[cid] = by_campaign.get(cid, 0) + float(r.get("amount") or 0)
        by_campaign_count[cid] = by_campaign_count.get(cid, 0) + 1
        method = str(r.get("payment_method") or "unknown")
        methods[method] = methods.get(method, 0) + 1
        created_raw = str(r.get("created_at") or "")
        try:
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            age = (now - created.astimezone(timezone.utc)).total_seconds()
            if age <= 7 * 86400:
                last_7 += 1
            if age <= 30 * 86400:
                last_30 += 1
        except Exception:
            pass
    top = sorted(
        [
            {
                "id": cid,
                "name": name_by_id.get(cid, "Unknown"),
                "status": status_by_id.get(cid, ""),
                "total": round(amt, 2),
                "count": by_campaign_count.get(cid, 0),
            }
            for cid, amt in by_campaign.items()
        ],
        key=lambda x: x["total"],
        reverse=True,
    )[:5]
    return {
        "campaign_count": len(campaigns),
        "live_campaign_count": sum(1 for c in campaigns if str(c.get("status") or "").lower() == "live"),
        "donation_count": len(ok),
        "total_amount": round(total, 2),
        "avg_gift": round(total / len(ok), 2) if ok else 0,
        "monthly_count": monthly,
        "once_count": max(0, len(ok) - monthly),
        "mobile_share": round((mobile / len(ok) * 100) if ok else 0, 1),
        "gifts_last_7_days": last_7,
        "gifts_last_30_days": last_30,
        "payment_methods": methods,
        "top_campaigns": top,
        "campaigns": campaigns,
        "focus_campaign_id": campaign_id,
        "sample_size": len(rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _fallback_ab(name: str) -> dict[str, Any]:
    return {
        "overall_score": 74,
        "summary": (
            f"“{name}” is conversion-ready with room to tighten the first screen. "
            "Focus on headline clarity and donation amount psychology before redesigning layout."
        ),
        "winning_focus": "Lead with a specific outcome in the headline and test a mid-tier default amount.",
        "items": [
            {
                "category": "Headlines",
                "score": 78,
                "verdict": "Strong emotion, slightly long",
                "suggestion": "Front-load the urgent outcome in the first 6–8 words; keep urgency without stacking clauses.",
            },
            {
                "category": "Images",
                "score": 72,
                "verdict": "Hero supports the story",
                "suggestion": "Prefer a single human-centered image above the fold; avoid busy collages on mobile.",
            },
            {
                "category": "Colors",
                "score": 80,
                "verdict": "Brand contrast is solid",
                "suggestion": "Keep primary CTA contrast high; mute secondary buttons so the donate action wins.",
            },
            {
                "category": "Buttons",
                "score": 70,
                "verdict": "CTA copy can be sharper",
                "suggestion": "Replace generic “Donate” with outcome language (e.g. “Feed a family today”).",
            },
            {
                "category": "Layouts",
                "score": 68,
                "verdict": "Mobile scroll depth is the risk",
                "suggestion": "Keep amount selection visible without requiring a long scroll after the story opens.",
            },
            {
                "category": "Donation amounts",
                "score": 76,
                "verdict": "Presets look balanced",
                "suggestion": "Highlight the middle amount as the recommended gift and label what it unlocks.",
            },
        ],
    }


def _fallback_analytics(campaign_name: str, snap: dict[str, Any]) -> dict[str, Any]:
    today = datetime.now(timezone.utc).strftime("%b %d, %Y")
    mobile = snap.get("mobile_share") or 0
    return {
        "date_label": f"Daily briefing · {today}",
        "campaign_name": campaign_name,
        "headline": (
            "Conversion dipped on mobile after the amount step — donors are hesitating on preset gifts."
            if mobile >= 40
            else "Traffic quality is steady; conversion is held back by weak post-amount messaging."
        ),
        "insights": [
            {
                "severity": "critical",
                "title": "Mobile drop after amount selection",
                "explanation": (
                    "Conversion dropped because mobile users are leaving after the donation amount selection. "
                    "Simplify presets, highlight one recommended amount, and keep the next step on the same screen."
                ),
            },
            {
                "severity": "warning",
                "title": "Checkout friction",
                "explanation": (
                    f"About {mobile}% of recent gifts look mobile. Long forms after amount selection hurt completion — "
                    "reduce required fields and surface wallet pay earlier."
                ),
            },
            {
                "severity": "positive",
                "title": "Recurring interest",
                "explanation": (
                    f"{snap.get('monthly_count') or 0} recent gifts are monthly. "
                    "Lean into monthly upsell copy right after a one-time amount is chosen."
                ),
            },
            {
                "severity": "info",
                "title": "Volume snapshot",
                "explanation": (
                    f"{snap.get('donation_count') or 0} countable donations in the latest sample "
                    f"(~{snap.get('total_amount') or 0} combined). Use this narrative beside charts, not instead of them."
                ),
            },
        ],
    }


def _fallback_dashboard(org_name: str, snap: dict[str, Any]) -> dict[str, Any]:
    top = snap.get("top_campaigns") or []
    best = [
        {
            "name": c["name"],
            "why": f"Leading recent volume (~{c['total']}) with stronger completion than peers.",
        }
        for c in top[:3]
    ] or [{"name": "No live campaigns yet", "why": "Publish a campaign to unlock comparisons."}]
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "revenue_summary": (
            f"{org_name} recent sample shows ~{snap.get('total_amount') or 0} across "
            f"{snap.get('donation_count') or 0} gifts. Momentum is concentrated in a few campaigns."
        ),
        "conversion_summary": (
            "Conversion is most fragile on mobile between amount selection and payment. "
            "Treat that step as the main leak to fix this week."
        ),
        "repeat_donors_summary": (
            f"{snap.get('monthly_count') or 0} recent gifts are recurring — protect retention with clear plan reminders "
            "and a thank-you that invites a second gift."
        ),
        "best_campaigns": best,
        "problems": [
            "Mobile donors abandon after choosing an amount.",
            "Some campaigns under-explain what each gift amount funds.",
            "CTA language is generic on weaker pages.",
        ],
        "recommended_actions": [
            "Run AI Helper on the top campaign and apply the highest-ROI suggestion first.",
            "Raise the middle preset and label it as the recommended gift.",
            "Add a one-line impact statement under the amount grid on mobile.",
            "Review AI Analytics daily and ship one fix before adding new traffic.",
        ],
    }


@router.post("/orgs/{org_id}/ai/ab-helper")
def ai_ab_helper(
    org_id: str,
    payload: AbHelperRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    bundle = _campaign_bundle(org_id, payload.campaign_id)
    campaign = bundle["campaign"]
    content = bundle["content"]
    name = str(campaign.get("name") or "Campaign")
    status = str(campaign.get("status") or "")
    perf = _campaign_performance(org_id, payload.campaign_id)
    presets = _campaign_amount_presets(payload.campaign_id)
    api_key, model = _require_openai()

    title = str(content.get("title") or name)
    body = str(content.get("body_html") or content.get("desktop_body") or "")[:3500]
    body_mobile = str(content.get("body_html_mobile") or content.get("mobile_body") or "")[:2000]
    popup = str(
        content.get("popup_body_html")
        or content.get("modal_body_html")
        or content.get("popup_body")
        or ""
    )[:1800]
    color = str(campaign.get("primary_color") or "")
    currency = str(campaign.get("default_currency") or "USD")

    messages = [
        {
            "role": "system",
            "content": (
                "You are a fundraising conversion coach. Score THIS campaign using real page copy "
                "AND real donation stats. Optimize for higher gift completion and average gift size. "
                "Return ONLY JSON with keys: overall_score (0-100 number), summary (2 short sentences), "
                "winning_focus (one concrete next edit a busy fundraiser can ship today), "
                "items (array of exactly 6 objects). "
                "Each item must include category, score (0-100), verdict (short), suggestion "
                "(one clear action with expected conversion benefit). "
                "Categories MUST be exactly: Headlines, Images, Colors, Buttons, Layouts, Donation amounts. "
                "Write plain language — no jargon. Prefer specific copy/amount changes over vague advice. "
                "Ground every suggestion in the supplied copy or stats. "
                "If gift volume is low, say so and prioritize clarity/presets over inventing traffic claims. "
                "Do not invent GA numbers. Do not invent donation counts."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "campaign": {
                        "name": name,
                        "status": status,
                        "title": title,
                        "primary_color": color,
                        "default_currency": currency,
                    },
                    "copy": {
                        "desktop_body_excerpt": body or "(empty)",
                        "mobile_body_excerpt": body_mobile or "(empty)",
                        "popup_excerpt": popup or "(empty)",
                    },
                    "amount_presets": presets,
                    "live_performance": perf,
                },
                ensure_ascii=False,
                default=str,
            ),
        },
    ]
    parsed = _openai_json(api_key, model, messages, max_tokens=2200)
    items = parsed.get("items") or []
    if not isinstance(items, list) or len(items) < 4:
        raise HTTPException(status_code=502, detail="AI returned an incomplete conversion review")
    return {
        "overall_score": float(parsed.get("overall_score") or 0),
        "summary": str(parsed.get("summary") or ""),
        "winning_focus": str(parsed.get("winning_focus") or ""),
        "items": [
            {
                "category": str(it.get("category") or "Item"),
                "score": float(it.get("score") or 0),
                "verdict": str(it.get("verdict") or ""),
                "suggestion": str(it.get("suggestion") or ""),
            }
            for it in items
            if isinstance(it, dict)
        ][:6],
        "source": "openai",
        "live": True,
        "model": model,
        "based_on": {
            "campaign_name": name,
            "campaign_status": status,
            "donation_count": perf.get("donation_count"),
            "gifts_last_30_days": perf.get("gifts_last_30_days"),
            "avg_gift": perf.get("avg_gift"),
            "mobile_share_pct": perf.get("mobile_share_pct"),
        },
    }


@router.post("/orgs/{org_id}/ai/analytics-explain")
def ai_analytics_explain(
    org_id: str,
    payload: AnalyticsExplainRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    snap = _org_donation_snapshot(org_id, payload.campaign_id)
    campaign_name = "All campaigns"
    if payload.campaign_id:
        match = next((c for c in snap["campaigns"] if c.get("id") == payload.campaign_id), None)
        campaign_name = (match or {}).get("name") or "Campaign"
        snap["focus_performance"] = _campaign_performance(org_id, payload.campaign_id)

    ga4 = _optional_ga4_snapshot(org_id, payload.campaign_id)
    if ga4:
        snap["ga4"] = ga4

    api_key, model = _require_openai()
    today = datetime.now(timezone.utc).strftime("%b %d, %Y")

    messages = [
        {
            "role": "system",
            "content": (
                "You write a daily fundraising briefing for busy nonprofit operators. "
                "Keep it easy to skim and focused on what to fix today to raise more. "
                "Return ONLY JSON: date_label, campaign_name, headline (one punchy sentence), "
                "insights (array of 4-6 objects: {severity, title, explanation}). "
                "severity must be one of: critical, warning, positive, info. "
                "Each explanation: 1-2 plain sentences ending with a concrete next step when useful. "
                "Use ONLY the provided donation stats (and GA4 if present). "
                "Do not invent pageviews, bounce rates, or donation counts. "
                "If volume is thin, say the sample is small and give cautious recommendations. "
                "Prefer causal explanations tied to real metrics like mobile share, recurring share, "
                "payment methods, 7/30-day gift counts, and top campaigns."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "range": payload.range,
                    "focus": campaign_name,
                    "today_utc": today,
                    "stats": snap,
                },
                ensure_ascii=False,
                default=str,
            ),
        },
    ]
    parsed = _openai_json(api_key, model, messages, max_tokens=1800)
    insights = parsed.get("insights") or []
    if not isinstance(insights, list) or not insights:
        raise HTTPException(status_code=502, detail="AI returned an empty analytics briefing")
    return {
        "date_label": str(parsed.get("date_label") or f"Daily briefing · {today}"),
        "campaign_name": str(parsed.get("campaign_name") or campaign_name),
        "headline": str(parsed.get("headline") or ""),
        "insights": [
            {
                "severity": str(it.get("severity") or "info"),
                "title": str(it.get("title") or ""),
                "explanation": str(it.get("explanation") or ""),
            }
            for it in insights
            if isinstance(it, dict)
        ][:8],
        "source": "openai",
        "live": True,
        "model": model,
        "based_on": {
            "donation_count": snap.get("donation_count"),
            "gifts_last_7_days": snap.get("gifts_last_7_days"),
            "gifts_last_30_days": snap.get("gifts_last_30_days"),
            "mobile_share": snap.get("mobile_share"),
            "ga4_included": bool(ga4),
        },
    }


@router.post("/orgs/{org_id}/ai/org-dashboard")
def ai_org_dashboard(
    org_id: str,
    payload: OrgDashboardRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    require_org_access(org_id, user, min_role="admin")
    org = rest_get_one("organizations", params={"id": f"eq.{org_id}", "select": "name"}) or {}
    org_name = str(org.get("name") or "Organization")
    snap = _org_donation_snapshot(org_id)
    ga4 = _optional_ga4_snapshot(org_id)
    if ga4:
        snap["ga4"] = ga4

    api_key, model = _require_openai()

    messages = [
        {
            "role": "system",
            "content": (
                "Summarize an organization fundraising dashboard for busy operators using REAL stats only. "
                "Write plain, conversion-focused language. "
                "Return ONLY JSON with keys: generated_at, revenue_summary, conversion_summary, "
                "repeat_donors_summary, best_campaigns (array of {name, why}), "
                "problems (string array of specific risks), "
                "recommended_actions (string array of high-ROI next steps, most important first). "
                "Each summary field: 2 short sentences max. Cite actual gift counts / amounts. "
                "Do not invent campaigns, revenue, or GA metrics that are not in the payload. "
                "If data is sparse, say so and recommend measuring next."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {"organization": org_name, "stats": snap},
                ensure_ascii=False,
                default=str,
            ),
        },
    ]
    parsed = _openai_json(api_key, model, messages, max_tokens=1800)
    best = parsed.get("best_campaigns") or []
    problems = parsed.get("problems") or []
    actions = parsed.get("recommended_actions") or []
    if not str(parsed.get("revenue_summary") or "").strip():
        raise HTTPException(status_code=502, detail="AI returned an incomplete organization summary")

    return {
        "generated_at": str(
            parsed.get("generated_at")
            or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        ),
        "revenue_summary": str(parsed.get("revenue_summary") or ""),
        "conversion_summary": str(parsed.get("conversion_summary") or ""),
        "repeat_donors_summary": str(parsed.get("repeat_donors_summary") or ""),
        "best_campaigns": [
            {"name": str(b.get("name") or ""), "why": str(b.get("why") or "")}
            for b in best
            if isinstance(b, dict)
        ][:5],
        "problems": [str(p) for p in problems][:6],
        "recommended_actions": [str(a) for a in actions][:6],
        "source": "openai",
        "live": True,
        "model": model,
        "based_on": {
            "donation_count": snap.get("donation_count"),
            "total_amount": snap.get("total_amount"),
            "live_campaign_count": snap.get("live_campaign_count"),
            "gifts_last_30_days": snap.get("gifts_last_30_days"),
            "ga4_included": bool(ga4),
        },
    }


def _parse_donation_dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _normalize_payment_method(value: Any) -> str:
    method = str(value or "unknown").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "credit_card": "card",
        "debit_card": "card",
        "creditcard": "card",
        "stripe": "card",
        "googlepay": "google_pay",
        "applepay": "apple_pay",
        "gpay": "google_pay",
        "crypto": "nowpayments",
        "now_payments": "nowpayments",
    }
    return aliases.get(method, method or "unknown")


def _utm_field(utm: Any, *keys: str) -> str:
    if not isinstance(utm, dict):
        return ""
    for key in keys:
        value = str(utm.get(key) or "").strip()
        if value:
            return value
    return ""


def _bump_count(bucket: dict[str, int], key: str) -> None:
    label = key or "(none)"
    bucket[label] = bucket.get(label, 0) + 1


def _top_counts(bucket: dict[str, int], *, limit: int = 12) -> list[dict[str, Any]]:
    return [
        {"label": k, "count": v}
        for k, v in sorted(bucket.items(), key=lambda x: x[1], reverse=True)[:limit]
    ]


def _insights_chat_context(org_id: str, campaign_id: str | None = None) -> dict[str, Any]:
    """Donation rollups for freeform analytics Q&A (counts, UTM, methods, campaigns)."""
    campaigns = rest_get(
        "campaigns",
        params={"organization_id": f"eq.{org_id}", "select": "id,name,status,slug"},
    ) or []
    name_by_id = {str(c["id"]): str(c.get("name") or "Campaign") for c in campaigns}
    params: dict[str, str] = {
        "organization_id": f"eq.{org_id}",
        "select": "amount,currency,frequency,created_at,campaign_id,status,device,payment_method,utm",
        "order": "created_at.desc",
        "limit": "800",
    }
    if campaign_id:
        params["campaign_id"] = f"eq.{campaign_id}"
    rows = rest_get("donations", params=params) or []
    ok = _countable_donations(rows)
    now = datetime.now(timezone.utc)

    by_method: dict[str, dict[str, float | int]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    by_campaign: dict[str, dict[str, Any]] = {}
    utm_source: dict[str, int] = {}
    utm_medium: dict[str, int] = {}
    utm_campaign: dict[str, int] = {}
    utm_term: dict[str, int] = {}
    utm_content: dict[str, int] = {}
    utm_combo: dict[str, int] = {}
    with_utm = 0
    without_utm = 0
    monthly = 0
    mobile = 0
    total = 0.0
    last_7 = 0
    last_30 = 0

    for r in ok:
        amt = float(r.get("amount") or 0)
        total += amt
        method = _normalize_payment_method(r.get("payment_method"))
        method_bucket = by_method.setdefault(method, {"count": 0, "total": 0.0})
        method_bucket["count"] = int(method_bucket["count"]) + 1
        method_bucket["total"] = round(float(method_bucket["total"]) + amt, 2)

        if r.get("frequency") == "monthly":
            monthly += 1
        if _is_mobile_device(r.get("device")):
            mobile += 1

        cid = str(r.get("campaign_id") or "")
        camp = by_campaign.setdefault(
            cid,
            {"id": cid, "name": name_by_id.get(cid, "Unknown"), "count": 0, "total": 0.0},
        )
        camp["count"] = int(camp["count"]) + 1
        camp["total"] = round(float(camp["total"]) + amt, 2)

        utm = r.get("utm") if isinstance(r.get("utm"), dict) else {}
        source = _utm_field(utm, "source", "utm_source")
        medium = _utm_field(utm, "medium", "utm_medium")
        campaign = _utm_field(utm, "campaign", "utm_campaign")
        term = _utm_field(utm, "term", "utm_term")
        content = _utm_field(utm, "content", "utm_content")
        if source or medium or campaign or term or content:
            with_utm += 1
            _bump_count(utm_source, source)
            _bump_count(utm_medium, medium)
            _bump_count(utm_campaign, campaign)
            _bump_count(utm_term, term)
            _bump_count(utm_content, content)
            combo = "|".join(
                [
                    f"source={source or '-'}",
                    f"medium={medium or '-'}",
                    f"campaign={campaign or '-'}",
                ]
            )
            _bump_count(utm_combo, combo)
        else:
            without_utm += 1

        created = _parse_donation_dt(r.get("created_at"))
        if not created:
            continue
        created_utc = created.astimezone(timezone.utc)
        day = created_utc.strftime("%Y-%m-%d")
        day_bucket = by_day.setdefault(
            day,
            {"date": day, "count": 0, "total": 0.0, "by_method": {}, "by_utm_source": {}},
        )
        day_bucket["count"] = int(day_bucket["count"]) + 1
        day_bucket["total"] = round(float(day_bucket["total"]) + amt, 2)
        day_methods: dict[str, Any] = day_bucket["by_method"]  # type: ignore[assignment]
        day_methods[method] = int(day_methods.get(method) or 0) + 1
        if source:
            day_sources: dict[str, Any] = day_bucket["by_utm_source"]  # type: ignore[assignment]
            day_sources[source] = int(day_sources.get(source) or 0) + 1

        age = (now - created_utc).total_seconds()
        if age <= 7 * 86400:
            last_7 += 1
        if age <= 30 * 86400:
            last_30 += 1

    days_sorted = sorted(by_day.values(), key=lambda x: str(x["date"]), reverse=True)[:45]
    campaigns_sorted = sorted(by_campaign.values(), key=lambda x: int(x["count"]), reverse=True)[:12]

    return {
        "timezone": "UTC",
        "generated_at": now.isoformat(),
        "focus_campaign_id": campaign_id,
        "focus_campaign_name": name_by_id.get(str(campaign_id or ""), None),
        "campaigns": [
            {"id": c.get("id"), "name": c.get("name"), "status": c.get("status")} for c in campaigns
        ],
        "totals": {
            "result_count": len(ok),
            "donation_count": len(ok),
            "total_amount": round(total, 2),
            "avg_gift": round(total / len(ok), 2) if ok else 0,
            "monthly_count": monthly,
            "once_count": max(0, len(ok) - monthly),
            "mobile_share_pct": round((mobile / len(ok) * 100) if ok else 0, 1),
            "gifts_last_7_days": last_7,
            "gifts_last_30_days": last_30,
            "with_utm_count": with_utm,
            "without_utm_count": without_utm,
            "sample_size": len(rows),
        },
        "utm": {
            "by_source": _top_counts(utm_source),
            "by_medium": _top_counts(utm_medium),
            "by_campaign": _top_counts(utm_campaign),
            "by_term": _top_counts(utm_term),
            "by_content": _top_counts(utm_content),
            "top_combos": _top_counts(utm_combo, limit=15),
        },
        "by_payment_method": [
            {"method": k, "count": int(v["count"]), "total": float(v["total"])}
            for k, v in sorted(by_method.items(), key=lambda x: int(x[1]["count"]), reverse=True)
        ],
        "by_day": days_sorted,
        "by_campaign": campaigns_sorted,
        "notes": (
            "result_count/donation_count = countable gift rows in sample. "
            "UTM fields come from donation.utm (source/medium/campaign/term/content). "
            "payment_method values include card, google_pay, apple_pay, paypal, nowpayments. "
            "'card' means Stripe card checkout. Dates are UTC calendar days (YYYY-MM-DD)."
        ),
    }


def _member_safe_chat_context(full: dict[str, Any]) -> dict[str, Any]:
    """Members may see UTM + result counts + insight mix — never donation amounts."""
    totals = full.get("totals") or {}
    return {
        "audience": "member",
        "timezone": full.get("timezone"),
        "generated_at": full.get("generated_at"),
        "focus_campaign_id": full.get("focus_campaign_id"),
        "focus_campaign_name": full.get("focus_campaign_name"),
        "campaigns": full.get("campaigns") or [],
        "totals": {
            "result_count": totals.get("result_count") or totals.get("donation_count") or 0,
            "monthly_count": totals.get("monthly_count") or 0,
            "once_count": totals.get("once_count") or 0,
            "mobile_share_pct": totals.get("mobile_share_pct") or 0,
            "results_last_7_days": totals.get("gifts_last_7_days") or 0,
            "results_last_30_days": totals.get("gifts_last_30_days") or 0,
            "with_utm_count": totals.get("with_utm_count") or 0,
            "without_utm_count": totals.get("without_utm_count") or 0,
            "sample_size": totals.get("sample_size") or 0,
        },
        "utm": full.get("utm") or {},
        "by_payment_method": [
            {"method": row.get("method"), "count": row.get("count")}
            for row in (full.get("by_payment_method") or [])
            if isinstance(row, dict)
        ],
        "by_day": [
            {
                "date": row.get("date"),
                "count": row.get("count"),
                "by_method": row.get("by_method") or {},
                "by_utm_source": row.get("by_utm_source") or {},
            }
            for row in (full.get("by_day") or [])
            if isinstance(row, dict)
        ],
        "by_campaign": [
            {"id": row.get("id"), "name": row.get("name"), "count": row.get("count")}
            for row in (full.get("by_campaign") or [])
            if isinstance(row, dict)
        ],
        "notes": (
            "MEMBER MODE: answer with UTM breakdowns, result counts, and insight analytics only. "
            "Never mention donation amounts, totals raised, average gift, currency, or revenue. "
            "If asked for money/donation values, refuse and explain that members only see counts and UTM."
        ),
    }


def _chat_access_mode(org_id: str, user: AuthUser) -> str:
    """admin = full money + counts; member = UTM/counts only."""
    if has_global_org_access(user):
        return "admin"
    role = str(user.org_roles.get(org_id) or "member").lower()
    if role in {"admin", "owner"}:
        return "admin"
    return "member"


def _cross_check_stats(context: dict[str, Any], *, mode: str) -> dict[str, Any]:
    """Compact numbers for humans to verify the bot against Insights/Donations."""
    totals = context.get("totals") or {}
    utm = context.get("utm") or {}
    check: dict[str, Any] = {
        "mode": mode,
        "generated_at": context.get("generated_at"),
        "focus_campaign_name": context.get("focus_campaign_name"),
        "result_count": totals.get("result_count") or totals.get("donation_count") or 0,
        "with_utm_count": totals.get("with_utm_count") or 0,
        "without_utm_count": totals.get("without_utm_count") or 0,
        "results_last_7_days": totals.get("results_last_7_days")
        or totals.get("gifts_last_7_days")
        or 0,
        "results_last_30_days": totals.get("results_last_30_days")
        or totals.get("gifts_last_30_days")
        or 0,
        "top_utm_sources": (utm.get("by_source") or [])[:5],
        "top_campaigns_by_count": (context.get("by_campaign") or [])[:5],
        "by_payment_method": (context.get("by_payment_method") or [])[:6],
        "recent_days": (context.get("by_day") or [])[:7],
    }
    if mode == "admin":
        check["total_amount"] = totals.get("total_amount")
        check["avg_gift"] = totals.get("avg_gift")
        check["donation_count"] = totals.get("donation_count")
    return check


@router.post("/orgs/{org_id}/ai/insights-chat")
def ai_insights_chat(
    org_id: str,
    payload: InsightsChatRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, Any]:
    """Ask plain-language questions about UTM, result counts, and (admins) donation money."""
    require_org_access(org_id, user, min_role="member")
    question = payload.message.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Message is required")

    mode = _chat_access_mode(org_id, user)

    if payload.campaign_id:
        _campaign_bundle(org_id, payload.campaign_id)

    full_context = _insights_chat_context(org_id, payload.campaign_id)
    context = full_context if mode == "admin" else _member_safe_chat_context(full_context)
    api_key, model = _require_openai()

    history_msgs: list[dict[str, str]] = []
    for item in payload.history[-12:]:
        role = str(item.role or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = str(item.content or "").strip()
        if not content:
            continue
        history_msgs.append({"role": role, "content": content[:2000]})

    if mode == "admin":
        system = (
            "You are an analytics assistant for a nonprofit fundraising ADMIN. "
            "Answer ONLY from the provided stats JSON. "
            "Be concise and specific with counts, amounts, UTM, payment methods, and dates. "
            "If asked about card gifts on a date, use by_day[].by_method.card for that YYYY-MM-DD. "
            "Cross-check yourself against totals/utm/by_day before answering. "
            "Never invent metrics. If data is thin, say so. "
            "Return ONLY JSON: {\"answer\": \"...\", \"highlights\": [\"optional short bullets\"]}."
        )
        context_label = "Full admin analytics context (JSON)"
    else:
        system = (
            "You are an analytics assistant for a nonprofit fundraising TEAM MEMBER. "
            "Answer ONLY from the provided MEMBER stats JSON. "
            "You may discuss: UTM parameters (source/medium/campaign/term/content), result counts, "
            "campaign result rankings by count, payment-method counts, device mix, and date counts. "
            "NEVER mention donation amounts, money raised, average gift, currency, revenue, or fees. "
            "If the user asks for donation amounts or money, refuse politely and offer count/UTM insights instead. "
            "Cross-check counts against totals/utm/by_day before answering. Never invent metrics. "
            "Return ONLY JSON: {\"answer\": \"...\", \"highlights\": [\"optional short bullets\"]}."
        )
        context_label = "Member-safe analytics context (JSON — counts & UTM only)"

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                f"{context_label}:\n"
                + json.dumps(context, ensure_ascii=False, default=str)
                + "\n\nUse this data for all answers."
            ),
        },
        *history_msgs,
        {"role": "user", "content": question},
    ]
    parsed = _openai_json(api_key, model, messages, max_tokens=900, temperature=0.2)
    answer = str(parsed.get("answer") or "").strip()
    if not answer:
        raise HTTPException(status_code=502, detail="AI returned an empty answer")
    highlights = parsed.get("highlights") or []
    if not isinstance(highlights, list):
        highlights = []

    response: dict[str, Any] = {
        "answer": answer,
        "highlights": [str(h) for h in highlights if str(h).strip()][:6],
        "source": "openai",
        "live": True,
        "model": model,
        "access_mode": mode,
        "based_on": {
            "result_count": (context.get("totals") or {}).get("result_count")
            or (context.get("totals") or {}).get("donation_count"),
            "days_covered": len(context.get("by_day") or []),
            "focus_campaign_id": payload.campaign_id,
            "focus_campaign_name": context.get("focus_campaign_name"),
            "with_utm_count": (context.get("totals") or {}).get("with_utm_count"),
        },
    }
    if payload.include_cross_check:
        response["stats_check"] = _cross_check_stats(context, mode=mode)
    return response


def localize_campaign_texts(
    target_language: str,
    texts: dict[str, str],
    language_name: str | None = None,
) -> dict[str, str]:
    raw = (target_language or "en").strip().replace("_", "-").lower()
    lang = (raw.split("-")[0] if raw else "en")[:8]
    if not lang or len(lang) < 2 or not lang.isalpha():
        raise HTTPException(status_code=400, detail="Invalid language code")
    if lang == "en":
        return dict(texts)

    api_key, model = _require_openai()
    known = {
        "ar": "Arabic",
        "fr": "French",
        "de": "German",
        "ur": "Urdu",
        "es": "Spanish",
        "tr": "Turkish",
        "pt": "Portuguese",
        "hi": "Hindi",
        "zh": "Chinese",
        "ja": "Japanese",
        "ko": "Korean",
        "ru": "Russian",
        "it": "Italian",
        "nl": "Dutch",
        "pl": "Polish",
        "bn": "Bengali",
        "fa": "Persian",
        "he": "Hebrew",
        "sw": "Swahili",
        "id": "Indonesian",
        "ms": "Malay",
        "th": "Thai",
        "vi": "Vietnamese",
        "uk": "Ukrainian",
        "ro": "Romanian",
        "cs": "Czech",
        "el": "Greek",
        "sv": "Swedish",
        "no": "Norwegian",
        "nb": "Norwegian",
        "da": "Danish",
        "fi": "Finnish",
        "hu": "Hungarian",
        "fil": "Filipino",
        "tl": "Filipino",
    }
    name = (language_name or "").strip() or known.get(lang) or lang
    messages = [
        {
            "role": "system",
            "content": (
                f"You localize fundraising campaign copy into {name} "
                f"(language code: {lang}). "
                "Use natural localized wording for donors — not literal translation. "
                "Preserve HTML tags, attributes, emojis, numbers, and placeholders exactly. "
                "Do not add new HTML. Keep roughly similar length so layout stays stable. "
                "Return ONLY JSON with the same keys you receive."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(texts, ensure_ascii=False),
        },
    ]
    parsed = _openai_json(api_key, model, messages, max_tokens=7000)
    out: dict[str, str] = {}
    for key, value in texts.items():
        translated = parsed.get(key)
        out[key] = str(translated) if translated is not None else value
    return out

