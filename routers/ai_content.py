from __future__ import annotations

import json
import os
import re
from html import escape
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import AuthUser, require_auth, require_org_access

router = APIRouter(prefix="/admin", tags=["ai-content"])

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
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


def _groq_json(api_key: str, model: str, messages: list[dict[str, str]], *, max_tokens: int = 1200) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.55,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    try:
        with httpx.Client(timeout=90.0) as client:
            response = client.post(
                GROQ_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="Cannot reach AI provider (Groq). Check your internet/DNS and try again.",
        ) from exc

    if response.status_code >= 400:
        detail = "AI provider error"
        try:
            err = response.json()
            detail = str((err.get("error") or {}).get("message") or err.get("message") or detail)
        except Exception:
            detail = response.text[:300] or detail
        raise HTTPException(status_code=502, detail=detail)

    try:
        data = response.json()
        raw = (((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
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
    parsed = _groq_json(api_key, model, messages, max_tokens=800)
    expanded = str(parsed.get("popup_body") or "").strip()
    return expanded or draft


@router.post("/orgs/{org_id}/ai/campaign-content")
def generate_campaign_content(
    org_id: str,
    payload: CampaignContentAiRequest,
    user: Annotated[AuthUser, Depends(require_auth)],
) -> dict[str, str]:
    require_org_access(org_id, user, min_role="admin")

    api_key = (os.getenv("GROQ_API_KEY") or "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="AI writing is not configured (missing GROQ_API_KEY)")

    model = (os.getenv("GROQ_MODEL") or DEFAULT_GROQ_MODEL).strip() or DEFAULT_GROQ_MODEL
    parsed = _groq_json(api_key, model, _build_messages(payload), max_tokens=4500)

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
