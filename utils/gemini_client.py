"""
Google Gemini client, via Google's OpenAI-compatibility endpoint — lets
this speak the exact same chat/completions shape as every other client in
utils/, instead of a bespoke generateContent() call shape just for this
one provider. One source of truth for "how we call an LLM" even across
genuinely different vendor APIs.

Free tier (as of this writing): Flash-class models only. gemini-2.5-pro
and other Pro-series models were moved to paid-only in April 2026 — don't
add a Pro model to MODEL_CANDIDATES without confirming it's still free on
this account first.

IMPORTANT — this is NOT a round-robin like Groq/Cerebras/OpenRouter.
Google enforces rate limits per Google Cloud PROJECT, not per API key. If
a second Gemini key ever gets added, it will NOT double the free quota
the way a second Groq or Cerebras key does — unless that key genuinely
belongs to a separate Google Cloud project. Don't build round-robin logic
here expecting Groq-style throughput multiplication from multiple keys on
the same account; it won't happen. Single key, single call, by design.
"""

import os
import logging

import aiohttp

from utils import http

logger = logging.getLogger("lucy.gemini_client")

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

# UNVERIFIED against this project's actual account — Google's free-tier
# model list has shifted before (Pro models pulled from free tier in
# April 2026). Confirm gemini-2.5-flash is still free for this key before
# relying on it for anything.
MODEL_CANDIDATES = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]


def _api_key() -> str:
    return os.getenv("GEMINI_API_KEY", "").strip()


async def call_gemini(messages: list[dict], model: str = MODEL_CANDIDATES[0], max_tokens: int = 700,
                       temperature: float = 0.85, timeout_seconds: int = 15) -> str:
    """Single-key call — see module docstring for why this isn't a
    round-robin like the other clients here. Raises RuntimeError on
    failure; caller decides whether/how to fall back to another provider."""
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set.")

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
    async with session.post(GEMINI_API_URL, json=payload, headers=headers, timeout=timeout) as resp:
        if resp.status == 429:
            raise RuntimeError(
                f"Gemini rate-limited on {model} (per-project limit, not per-key — "
                "adding another Gemini key from this same account won't help)"
            )
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"Gemini {model} returned {resp.status}: {body[:300]}")
        data = await resp.json()

    try:
        content = (data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError("Gemini returned an unexpected response shape") from e
    if not content:
        raise RuntimeError(f"Gemini {model} returned empty content")
    return content


def is_configured() -> bool:
    return bool(_api_key())