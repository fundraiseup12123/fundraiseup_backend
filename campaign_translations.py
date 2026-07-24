"""Server-side campaign story translation cache (instant public language toggle)."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from db import rest_get_one, rest_insert

logger = logging.getLogger(__name__)

# Pre-warm these languages after campaign content saves.
WARM_LANGS = ("ur", "ar")

TEXT_KEYS = (
    "title",
    "titleHtml",
    "titleHtmlMobile",
    "caption",
    "captionMobile",
    "bodyHtml",
    "bodyHtmlMobile",
    "dedicationHint",
    "landingHeadlineHtml",
    "landingBodyHtml",
    "modalTitle",
    "modalTitleHtml",
    "modalBodyHtml",
    "modalTitleMobile",
    "modalTitleHtmlMobile",
    "modalBodyHtmlMobile",
)

# Short fields — translate first so the first toggle feels fast.
PRIORITY_KEYS = (
    "title",
    "titleHtml",
    "titleHtmlMobile",
    "caption",
    "captionMobile",
    "dedicationHint",
    "landingHeadlineHtml",
    "modalTitle",
    "modalTitleHtml",
    "modalTitleMobile",
    "modalTitleHtmlMobile",
)

BODY_KEYS = (
    "bodyHtml",
    "bodyHtmlMobile",
    "landingBodyHtml",
    "modalBodyHtml",
    "modalBodyHtmlMobile",
)


def normalize_lang(value: str | None) -> str:
    raw = (value or "en").strip().replace("_", "-").lower()
    primary = (raw.split("-")[0] if raw else "en")[:8]
    if not primary or len(primary) < 2 or not primary.isalpha():
        return "en"
    return primary


def content_fingerprint(texts: dict[str, Any]) -> str:
    """Match frontend contentFingerprint (unsigned 32-bit FNV-1a → base36)."""
    s = "\n".join(
        [
            str(texts.get("title") or ""),
            str(texts.get("caption") or ""),
            str(texts.get("modalTitle") or ""),
            str(texts.get("landingHeadlineHtml") or ""),
            str(texts.get("modalBodyHtml") or "")[:120],
            str(texts.get("bodyHtml") or "")[:120],
        ]
    )
    h = 2166136261
    for ch in s:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if h == 0:
        return "0"
    out: list[str] = []
    n = h
    while n:
        n, r = divmod(n, 36)
        out.append(alphabet[r])
    return "".join(reversed(out))


def texts_from_campaign_row(campaign: dict[str, Any] | None, content: dict[str, Any] | None) -> dict[str, str]:
    campaign = campaign or {}
    content = content or {}
    popup: dict[str, Any] = {}
    raw_pv = content.get("popup_view_json")
    if isinstance(raw_pv, dict):
        popup = raw_pv
    elif isinstance(raw_pv, str) and raw_pv.strip():
        try:
            parsed = json.loads(raw_pv)
            if isinstance(parsed, dict):
                popup = parsed
        except Exception:
            popup = {}

    title = str(content.get("title") or campaign.get("name") or "")
    title_html = str(content.get("title_html") or title)
    body = str(content.get("body_html") or "")
    caption = str(content.get("caption") or "")

    return {
        "title": title,
        "titleHtml": title_html,
        "titleHtmlMobile": str(content.get("title_html_mobile") or title_html),
        "caption": caption,
        "captionMobile": str(content.get("caption_mobile") or caption),
        "bodyHtml": body,
        "bodyHtmlMobile": str(content.get("body_html_mobile") or body),
        "dedicationHint": str(content.get("dedication_hint") or ""),
        "landingHeadlineHtml": str(popup.get("landing_headline_html") or popup.get("landingHeadlineHtml") or ""),
        "landingBodyHtml": str(popup.get("landing_body_html") or popup.get("landingBodyHtml") or ""),
        "modalTitle": str(popup.get("modal_title") or popup.get("modalTitle") or ""),
        "modalTitleHtml": str(
            popup.get("modal_title_html") or popup.get("modalTitleHtml") or popup.get("modal_title") or ""
        ),
        "modalBodyHtml": str(popup.get("modal_body_html") or popup.get("modalBodyHtml") or ""),
        "modalTitleMobile": str(popup.get("modal_title_mobile") or popup.get("modalTitleMobile") or ""),
        "modalTitleHtmlMobile": str(
            popup.get("modal_title_html_mobile") or popup.get("modalTitleHtmlMobile") or ""
        ),
        "modalBodyHtmlMobile": str(
            popup.get("modal_body_html_mobile") or popup.get("modalBodyHtmlMobile") or ""
        ),
    }


def get_cached_translation(
    campaign_id: str,
    lang: str,
    content_fp: str | None = None,
) -> dict[str, Any] | None:
    lang = normalize_lang(lang)
    if lang == "en" or not campaign_id:
        return None
    row = rest_get_one(
        "campaign_translations",
        params={
            "campaign_id": f"eq.{campaign_id}",
            "lang": f"eq.{lang}",
            "select": "campaign_id,lang,content_fp,texts,ui_strings,updated_at",
        },
    )
    if not row:
        return None
    if content_fp is not None and str(row.get("content_fp") or "") != str(content_fp):
        return None
    texts = row.get("texts") or {}
    ui_strings = row.get("ui_strings") or {}
    if isinstance(texts, str):
        try:
            texts = json.loads(texts)
        except Exception:
            texts = {}
    if isinstance(ui_strings, str):
        try:
            ui_strings = json.loads(ui_strings)
        except Exception:
            ui_strings = {}
    if not isinstance(texts, dict):
        return None
    return {
        "campaign_id": campaign_id,
        "target_language": lang,
        "content_fp": str(row.get("content_fp") or ""),
        "texts": {str(k): str(v or "") for k, v in texts.items()},
        "ui_strings": {str(k): str(v or "") for k, v in (ui_strings or {}).items()}
        if isinstance(ui_strings, dict)
        else {},
        "cached": True,
    }


def store_cached_translation(
    campaign_id: str,
    lang: str,
    content_fp: str,
    texts: dict[str, str],
    ui_strings: dict[str, str] | None = None,
) -> None:
    lang = normalize_lang(lang)
    if lang == "en" or not campaign_id:
        return
    row = {
        "campaign_id": campaign_id,
        "lang": lang,
        "content_fp": content_fp or "",
        "texts": texts or {},
        "ui_strings": ui_strings or {},
    }
    try:
        rest_insert("campaign_translations", row, on_conflict="campaign_id,lang")
    except Exception as exc:
        logger.warning("Failed to store campaign translation cache: %s", exc)


def translate_and_cache(
    *,
    campaign_id: str,
    target_language: str,
    texts: dict[str, str],
    ui_strings: dict[str, str] | None = None,
    language_name: str | None = None,
    force: bool = False,
    priority_only: bool = False,
    bodies_only: bool = False,
) -> dict[str, Any]:
    """Return cached translation or generate via OpenAI and store.

    priority_only=True translates short headlines/captions only (fast first paint).
    bodies_only=True translates long HTML bodies in parallel (follow-up fill).
    Full pass translates everything and writes the server cache.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from routers.ai_content import localize_campaign_texts

    lang = normalize_lang(target_language)
    clean_texts = {k: str(texts.get(k) or "") for k in TEXT_KEYS}
    for key, value in texts.items():
        clean_texts.setdefault(str(key), str(value or ""))
    fp = content_fingerprint(clean_texts)
    ui_in = {str(k): str(v) for k, v in (ui_strings or {}).items() if str(v).strip()}

    if not force and not bodies_only:
        cached = get_cached_translation(campaign_id, lang, fp)
        if cached:
            return cached

    if lang == "en":
        return {
            "campaign_id": campaign_id,
            "target_language": lang,
            "content_fp": fp,
            "texts": clean_texts,
            "ui_strings": ui_in,
            "cached": False,
            "partial": False,
        }

    def _localize(chunk: dict[str, str], max_tokens: int) -> dict[str, str]:
        if not chunk:
            return {}
        return localize_campaign_texts(
            lang,
            chunk,
            language_name=language_name,
            max_tokens=max_tokens,
        )

    if priority_only:
        priority_src = {
            k: clean_texts[k] for k in PRIORITY_KEYS if str(clean_texts.get(k) or "").strip()
        }
        merged = {f"c__{k}": v for k, v in priority_src.items()}
        localized_merged = _localize(merged, 1800)
        localized = dict(clean_texts)  # body stays English for now
        # Clear priority keys so omitted AI fields stay empty — client falls back to
        # related translated fields (e.g. mobile title → desktop title) instead of
        # treating leftover English as a successful translation.
        for key in priority_src:
            localized[key] = ""
        for key, value in localized_merged.items():
            if key.startswith("c__"):
                text = str(value or "").strip()
                if text:
                    localized[key[3:]] = text
        return {
            "campaign_id": campaign_id,
            "target_language": lang,
            "content_fp": fp,
            "texts": localized,
            "ui_strings": ui_in,
            "cached": False,
            "partial": True,
        }

    if bodies_only:
        body_src = {
            k: clean_texts[k] for k in BODY_KEYS if str(clean_texts.get(k) or "").strip()
        }
        localized = dict(clean_texts)
        if body_src:
            # Parallel per-field calls — much faster wall-clock than one giant body blob.
            with ThreadPoolExecutor(max_workers=min(4, len(body_src))) as pool:
                futures = {
                    pool.submit(
                        _localize,
                        {f"c__{key}": value},
                        2800 if len(value) < 4000 else 4500,
                    ): key
                    for key, value in body_src.items()
                }
                for fut in as_completed(futures):
                    key = futures[fut]
                    try:
                        chunk_out = fut.result()
                        translated = chunk_out.get(f"c__{key}")
                        if translated is not None and str(translated).strip():
                            localized[key] = str(translated)
                    except Exception as exc:
                        logger.warning("Body field %s translate failed: %s", key, exc)
        return {
            "campaign_id": campaign_id,
            "target_language": lang,
            "content_fp": fp,
            "texts": localized,
            "ui_strings": ui_in,
            "cached": False,
            "partial": True,
            "bodies_only": True,
        }

    # Full pass: short fields + bodies in parallel, then cache.
    short_src = {
        k: clean_texts[k]
        for k in TEXT_KEYS
        if k not in BODY_KEYS and str(clean_texts.get(k) or "").strip()
    }
    body_src = {
        k: clean_texts[k] for k in BODY_KEYS if str(clean_texts.get(k) or "").strip()
    }
    localized: dict[str, str] = {}
    ui_out: dict[str, str] = {}

    jobs: list[tuple[str, dict[str, str], int]] = []
    short_chunk: dict[str, str] = {f"c__{k}": v for k, v in short_src.items()}
    for key, value in ui_in.items():
        short_chunk[f"u__{key}"] = value
    if short_chunk:
        jobs.append(("short", short_chunk, 2200))
    for key, value in body_src.items():
        jobs.append(
            (
                key,
                {f"c__{key}": value},
                2800 if len(value) < 4000 else 4500,
            )
        )

    with ThreadPoolExecutor(max_workers=min(5, max(1, len(jobs)))) as pool:
        futures = {
            pool.submit(_localize, chunk, tokens): label for label, chunk, tokens in jobs
        }
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                chunk_out = fut.result()
            except Exception as exc:
                logger.warning("Translate chunk %s failed: %s", label, exc)
                continue
            for key, value in chunk_out.items():
                if key.startswith("c__"):
                    localized[key[3:]] = str(value)
                elif key.startswith("u__"):
                    ui_out[key[3:]] = str(value)

    for key, value in ui_in.items():
        ui_out.setdefault(key, value)
    for key, value in clean_texts.items():
        localized.setdefault(key, value)

    store_cached_translation(campaign_id, lang, fp, localized, ui_out)
    return {
        "campaign_id": campaign_id,
        "target_language": lang,
        "content_fp": fp,
        "texts": localized,
        "ui_strings": ui_out,
        "cached": False,
        "partial": False,
    }


def warm_campaign_translations(campaign_id: str, langs: tuple[str, ...] = WARM_LANGS) -> None:
    """Best-effort warm for priority languages (runs in a background thread)."""
    campaign = rest_get_one("campaigns", params={"id": f"eq.{campaign_id}", "select": "id,name"})
    content = rest_get_one("campaign_content", params={"campaign_id": f"eq.{campaign_id}", "select": "*"})
    if not campaign:
        return
    texts = texts_from_campaign_row(campaign, content)
    for lang in langs:
        try:
            translate_and_cache(campaign_id=campaign_id, target_language=lang, texts=texts)
        except Exception as exc:
            logger.warning("Warm translation failed for %s/%s: %s", campaign_id, lang, exc)


def warm_campaign_translations_async(campaign_id: str) -> None:
    if not campaign_id:
        return

    def _run() -> None:
        try:
            warm_campaign_translations(campaign_id)
        except Exception as exc:
            logger.warning("Background translation warm failed: %s", exc)

    threading.Thread(target=_run, daemon=True, name=f"warm-i18n-{campaign_id[:8]}").start()
