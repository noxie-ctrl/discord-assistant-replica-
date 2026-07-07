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

import hashlib
import itertools
import logging
import os
from typing import Any, Dict, List

import aiohttp

logger = logging.getLogger("lucy.openrouter")

OPENROUTER_API_URL = os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")
DEFAULT_CHAT_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
DEFAULT_VISION_MODEL = os.getenv("OPENROUTER_VISION_MODEL", DEFAULT_CHAT_MODEL)

_key_cycle = None


class _RateLimited(Exception):
    """Raised when an OpenRouter key returns HTTP 429."""


def _get_keys() -> List[str]:
    keys = [
        os.getenv("OPENROUTER_API_KEY", "").strip(),
        os.getenv("OPENROUTER_API_KEY_2", "").strip(),
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
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(OPENROUTER_API_URL, json=payload, headers=headers) as resp:
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
                          timeout_seconds: int = 12) -> str:
    """Call OpenRouter with round-robin API key handling.

    Raises RuntimeError if no keys configured or all keys fail.
    """
    keys = _next_key_order()
    if not keys:
        raise RuntimeError("No OPENROUTER_API_KEY configured.")

    last_error: Exception | None = None
    for key in keys:
        try:
            return await _call_one(model, messages, max_tokens, temperature, key, timeout_seconds)
        except asyncio.TimeoutError:
            last_error = TimeoutError(f"OpenRouter {model} timed out")
            logger.warning("OpenRouter key timed out on %s, trying next key if any", model)
        except _RateLimited as e:
            last_error = e
            logger.warning("OpenRouter key rate-limited, trying next key if any")
        except Exception as e:
            last_error = e
            logger.warning("OpenRouter call failed (%s), trying next key if any", e)

    raise RuntimeError(f"All OpenRouter keys failed: {last_error}")


def _make_cache_key(urls: List[str]) -> str:
    """Create a deterministic short cache key for a list of image URLs."""
    joined = "|".join(urls)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


async def describe_images(image_urls: List[str], prompt: str = "Describe the image(s) plainly and briefly.") -> str:
    """Return a short description for the given image URLs.

    Uses the DB cache if available, and stores results back into the cache.
    Raises RuntimeError if the model signals the content is blocked.
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

    result = await call_openrouter(messages, model=DEFAULT_VISION_MODEL, max_tokens=220, temperature=0.2)
    if result.strip().upper() == "SAFETY_BLOCKED":
        raise RuntimeError("OpenRouter safety blocked image description")
    desc = result.strip()

    # Store in cache (best-effort)
    try:
        from utils import database as db

        await db.set_image_description(cache_key, desc)
    except Exception:
        logger.debug("OpenRouter: failed to write cache for %s", cache_key)

    return desc


def is_configured() -> bool:
    """Return True if any OpenRouter key is configured."""
    return bool(_get_keys())
