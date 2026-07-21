"""OpenRouter client utilities.

Provides a thin wrapper around OpenRouter's chat/completions API for two
purposes used in this project:
- `call_openrouter`: generic chat-style calls used for fallback text tasks.
- `describe_images`: a vision-oriented helper that accepts a list of image URLs
  and returns a short plain-language description. Results are cached in the
  project's Postgres DB (if configured) to avoid repeated API calls for the
  same images.

This module is deliberately lightweight and defensive: missing env keys or a
missing DB do not cause import-time failures; callers must handle runtime
errors when the external APIs are not available.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import os
import re
from typing import Any, Dict, List

import aiohttp

from utils import http

logger = logging.getLogger("lucy.openrouter")

OPENROUTER_API_URL = os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")
# "openrouter/free" is OpenRouter's own free-model router, not a single pinned
# model: it auto-selects from whatever's currently free (including vision-
# capable models when the request includes an image), at $0/token. Free
# model *names* rotate constantly (things lose :free status without notice),
# so pinning a specific one here would go stale — this router sidesteps that
# and never requires OpenRouter credits. Override via env var if you want a
# specific pinned model instead (e.g. a paid one for quality).
DEFAULT_CHAT_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
DEFAULT_VISION_MODEL = os.getenv("OPENROUTER_VISION_MODEL", "openrouter/free")

_key_cycle = None


class _RateLimited(Exception):
    """Raised when an OpenRouter key returns HTTP 429."""


def _get_keys() -> List[str]:
    keys = [
        os.getenv("OPENROUTER_API_KEY", "").strip(),
        os.getenv("OPENROUTER_API_KEY_2", "").strip(),
        os.getenv("OPENROUTER_API_KEY_3", "").strip(),
        os.getenv("OPENROUTER_API_KEY_4", "").strip(),
        os.getenv("OPENROUTER_API_KEY_5", "").strip(),
    ]
    return [k for k in keys if k]


def _next_key_order() -> List[str]:
    """Return available keys in round-robin order for load distribution."""
    global _key_cycle
    keys = _get_keys()
    if not keys:
        return []
    if _key_cycle is None:
        _key_cycle = itertools.cycle(range(len(keys)))
    start = next(_key_cycle)
    return keys[start:] + keys[:start]


async def _call_one(model: str, messages: List[Dict[str, Any]], max_tokens: int, temperature: float,
                    api_key: str, timeout_seconds: int = 12) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    session = await http.get_session()
    async with session.post(OPENROUTER_API_URL, json=payload, headers=headers, timeout=timeout) as resp:
        if resp.status == 429:
            raise _RateLimited(f"OpenRouter key rate-limited on {model}")
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"OpenRouter {model} returned {resp.status}: {body[:300]}")
        data = await resp.json()

    try:
        # Support typical OpenAI-compatible shape: choices[0].message.content
        content = (data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError("OpenRouter returned an unexpected response shape") from e
    if not content:
        raise RuntimeError(f"OpenRouter {model} returned empty content")
    return content


async def call_openrouter(messages: List[Dict[str, Any]], model: str = DEFAULT_CHAT_MODEL,
                          max_tokens: int = 400, temperature: float = 0.3,
                          timeout_seconds: int = 12, max_rounds: int = 2) -> str:
    """Call OpenRouter with round-robin API key handling.

    Live testing surfaced a real gap: free-tier vision calls hitting a 429
    or a timeout had exactly one shot per key, then gave up outright — no
    room for a transient blip to clear. `max_rounds` re-cycles through all
    keys again (with a short backoff) specifically when the failures seen
    were transient (rate limit / timeout); a hard error (bad request, model
    rejected the content, etc.) still only gets one pass since retrying
    those wouldn't change the outcome.

    Raises RuntimeError if no keys configured or all keys/rounds fail.
    """
    keys = _next_key_order()
    if not keys:
        raise RuntimeError("No OPENROUTER_API_KEY configured.")

    last_error: Exception | None = None
    for round_num in range(max_rounds):
        if round_num > 0:
            logger.warning("Retrying OpenRouter after transient failures (round %d)", round_num + 1)
            await asyncio.sleep(1.5 * round_num)

        saw_transient_failure = False
        for key in keys:
            try:
                return await _call_one(model, messages, max_tokens, temperature, key, timeout_seconds)
            except asyncio.TimeoutError:
                last_error = TimeoutError(f"OpenRouter {model} timed out")
                saw_transient_failure = True
                logger.warning("OpenRouter key timed out on %s, trying next key if any", model)
            except _RateLimited as e:
                last_error = e
                saw_transient_failure = True
                logger.warning("OpenRouter key rate-limited, trying next key if any")
            except Exception as e:
                last_error = e
                logger.warning("OpenRouter call failed (%s), trying next key if any", e)

        if not saw_transient_failure:
            break  # failures were hard errors — another round won't help

    raise RuntimeError(f"All OpenRouter keys failed: {last_error}")


def _make_cache_key(urls: List[str]) -> str:
    """Create a deterministic short cache key for a list of image URLs."""
    joined = "|".join(urls)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# "openrouter/free" auto-routes to whatever's currently free, and that
# rotation includes models that DON'T actually support vision. When one of
# those gets picked for an image request, it doesn't error — it answers
# with something that looks like real output but never looked at the image:
# a bare safety/moderation classification ("User Safety: safe"), a refusal,
# or a generic non-answer. If describe_images returned that as-is, the
# caller (and the model composing the final chat reply) has no way to tell
# it apart from a real description — and live testing showed the model
# will confabulate a plausible-sounding description from the filename/
# username instead of relaying the honest garbage, which is exactly the
# failure TOOL_RESULT_HONESTY_ADDENDUM is supposed to prevent. This check
# is a floor, not a guarantee: catches the two failure shapes actually
# observed, real descriptions are almost never this short or this-shaped.
_NON_DESCRIPTION_PATTERNS = (
    re.compile(r"^\s*(user\s+)?safety\s*:", re.IGNORECASE),
    re.compile(r"^\s*(the\s+)?content\s+is\s+safe\.?\s*$", re.IGNORECASE),
    re.compile(r"^\s*(i\s+)?(can'?t|cannot|am unable to)\s+(see|view|access|analyze)", re.IGNORECASE),
)
_MIN_DESCRIPTION_LENGTH = 15


def _looks_like_real_description(text: str) -> bool:
    stripped = (text or "").strip()
    if len(stripped) < _MIN_DESCRIPTION_LENGTH:
        return False
    return not any(pattern.match(stripped) for pattern in _NON_DESCRIPTION_PATTERNS)


async def describe_images(image_urls: List[str], prompt: str = "Describe the image(s) plainly and briefly.") -> str:
    """Return a short description for the given image URLs.

    Uses the DB cache if available, and stores results back into the cache.
    Raises RuntimeError if the model signals the content is blocked, or if
    every retry came back looking like a non-vision-model artifact rather
    than a real description (see _looks_like_real_description above) — the
    caller (ai_chat.py's describe_member_avatar branch) turns that into an
    honest "couldn't look at it right now" instead of relaying garbage.
    """
    if not image_urls:
        return ""

    cache_key = _make_cache_key(image_urls)
    # Try cache (best-effort)
    try:
        from utils import database as db

        cached = await db.get_image_description(cache_key)
        if cached:
            logger.debug("OpenRouter: cache hit for %s", cache_key)
            return cached
    except Exception:
        cached = None

    messages = [
        {
            "role": "system",
            "content": (
                "You describe images for a Discord bot. Reply with a plain-language summary "
                "of what the image contains, keeping it brief and factual. If the content is "
                "safety-flagged or not appropriate to describe, respond with: SAFETY_BLOCKED"
            ),
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                *[
                    {"type": "image_url", "image_url": {"url": url}}
                    for url in image_urls
                ],
            ],
        },
    ]

    # openrouter/free can hand this request to a different underlying model
    # on each call, so a retry has a real chance of landing on one that
    # actually supports vision — this loop is separate from call_openrouter's
    # own transient-failure retries (429/timeout), it's specifically for "the
    # call succeeded but didn't actually describe anything."
    desc = None
    last_bad = None
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        result = await call_openrouter(messages, model=DEFAULT_VISION_MODEL, max_tokens=220, temperature=0.2)
        if result.strip().upper() == "SAFETY_BLOCKED":
            raise RuntimeError("OpenRouter safety blocked image description")
        candidate = result.strip()
        if _looks_like_real_description(candidate):
            desc = candidate
            break
        last_bad = candidate
        logger.warning(
            "OpenRouter vision call (attempt %d/%d) returned a non-description response "
            "(likely a non-vision free-router pick): %r",
            attempt, max_attempts, candidate,
        )

    if desc is None:
        raise RuntimeError(
            f"OpenRouter vision kept returning unusable responses after {max_attempts} attempts "
            f"(last: {last_bad!r}) — likely the free router keeps landing on a non-vision model."
        )

    # Store in cache (best-effort)
    try:
        from utils import database as db

        await db.set_image_description(cache_key, desc)
    except Exception:
        logger.debug("OpenRouter: failed to write cache for %s", cache_key)

    return desc


# ---------------------------------------------------------------------------
# Page OCR — used by utils/aysa_knowledge.py when a PDF page has no
# extractable text layer (a photographed/scanned page). Same free vision
# router as describe_images above, just a transcription prompt instead of a
# description one, and a much bigger max_tokens since a full book page of
# text is nowhere near a 220-token image caption. Deliberately a separate
# function rather than reusing describe_images: different prompt, different
# output-validity shape (a real transcription can legitimately be short —
# a title page — where a 15-char-minimum description check would wrongly
# reject it), and no DB caching (every page is unique, nothing to cache).
# ---------------------------------------------------------------------------

_NO_TEXT_SENTINEL = "NO_TEXT_FOUND"

_OCR_SYSTEM_PROMPT = (
    "You transcribe photographed/scanned book pages for a Discord bot's knowledge library. "
    "Reply with ONLY the page's text, transcribed as accurately as possible — preserve "
    "paragraph breaks, fix obvious OCR-unambiguous line-wrap hyphenation, but don't summarize, "
    "comment on, or add anything not on the page. Ignore page numbers, running headers/footers, "
    "and pure decoration. If the page is blank, is a cover/divider with no body text, or the "
    f"image has no legible text at all, reply with exactly: {_NO_TEXT_SENTINEL}. If the content "
    "is safety-flagged, reply with exactly: SAFETY_BLOCKED"
)


def _looks_like_real_transcription(text: str) -> bool:
    """Looser than _looks_like_real_description above — a genuine page can
    legitimately transcribe to something short (a chapter title page), so
    this only screens for the non-vision-model failure shapes actually
    observed, not a minimum length."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    return not any(pattern.match(stripped) for pattern in _NON_DESCRIPTION_PATTERNS)


async def ocr_page_text(image_data_uri: str, max_attempts: int = 3) -> str:
    """Transcribes one page image (a `data:image/png;base64,...` URI —
    OpenRouter's OpenAI-compatible endpoint accepts base64 data URLs the
    same as remote image URLs, so no image hosting is needed for a
    locally-rendered PDF page). Returns "" for a legitimately blank/text-
    free page (see _NO_TEXT_SENTINEL above) — that's a normal outcome, not
    an error. Raises RuntimeError if OpenRouter isn't configured, the page
    is safety-blocked, or every retry lands on a non-vision model — callers
    (utils/aysa_knowledge.py) catch this per-page and treat it as 'couldn't
    OCR this one', not a reason to abort the whole book."""
    if not is_configured():
        raise RuntimeError("No OPENROUTER_API_KEY configured — OCR fallback is unavailable.")

    messages = [
        {"role": "system", "content": _OCR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Transcribe this page."},
                {"type": "image_url", "image_url": {"url": image_data_uri}},
            ],
        },
    ]

    last_bad = None
    for attempt in range(1, max_attempts + 1):
        result = await call_openrouter(
            messages, model=DEFAULT_VISION_MODEL, max_tokens=2000, temperature=0.0, timeout_seconds=25,
        )
        candidate = result.strip()
        if candidate.upper() == "SAFETY_BLOCKED":
            raise RuntimeError("OpenRouter safety blocked page OCR")
        if candidate.upper() == _NO_TEXT_SENTINEL:
            return ""
        if _looks_like_real_transcription(candidate):
            return candidate
        last_bad = candidate
        logger.warning(
            "OpenRouter OCR call (attempt %d/%d) returned a non-transcription response "
            "(likely a non-vision free-router pick): %r",
            attempt, max_attempts, candidate,
        )

    raise RuntimeError(
        f"OpenRouter OCR kept returning unusable responses after {max_attempts} attempts "
        f"(last: {last_bad!r}) — likely the free router keeps landing on a non-vision model."
    )


def is_configured() -> bool:
    """Return True if any OpenRouter key is configured."""
    return bool(_get_keys())