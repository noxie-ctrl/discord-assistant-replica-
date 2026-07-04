"""
utils/groq_client.py

Groq is the "second engine": free, very fast, OpenAI-compatible REST API.
We use it for two things:
  1. Background/cheap tasks that used to eat into the main NIM quota —
     vent-channel triage, long-term memory summarization, and the daily
     news digest. These don't need Lucy's absolute best model, just a fast
     reliable one, so keeping them off NIM leaves more NIM headroom for
     actual conversations.
  2. A last-resort fallback for main chat, tried only if every NIM model in
     nim_client.MODEL_CANDIDATES has already failed — so a full NVIDIA
     outage doesn't take Lucy down completely, it just quietly degrades to
     a slightly different voice for a bit.

Two keys (GROQ_API_KEY_1 / GROQ_API_KEY_2) are round-robined per call, and
if one is rate-limited (429) the other is tried immediately in the same
call instead of waiting — effectively close to doubling free-tier
throughput for these background tasks.
"""

import os
import asyncio
import itertools
import logging

import aiohttp

logger = logging.getLogger("lucy.groq_client")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Fast + cheap — right for triage/summarization background work.
MODEL_FAST = "llama-3.1-8b-instant"
# Bigger — used when a background task actually benefits from more
# reasoning (news digest condensation) or as the main-chat fallback tier.
MODEL_QUALITY = "llama-3.3-70b-versatile"

_key_cycle = None


def _get_keys() -> list[str]:
    keys = [
        os.getenv("GROQ_API_KEY_1", "").strip(),
        os.getenv("GROQ_API_KEY_2", "").strip(),
    ]
    return [k for k in keys if k]


def _next_key_order() -> list[str]:
    """Returns the available keys starting from wherever the round-robin
    cursor currently is, so load spreads across both keys over time."""
    global _key_cycle
    keys = _get_keys()
    if not keys:
        return []
    if _key_cycle is None:
        _key_cycle = itertools.cycle(range(len(keys)))
    start = next(_key_cycle)
    return keys[start:] + keys[:start]


async def _call_one(model: str, messages: list[dict], max_tokens: int, temperature: float,
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
        async with session.post(GROQ_API_URL, json=payload, headers=headers) as resp:
            if resp.status == 429:
                raise _RateLimited(f"Groq key rate-limited on {model}")
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Groq {model} returned {resp.status}: {body[:300]}")
            data = await resp.json()

    try:
        content = (data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError("Groq returned an unexpected response shape") from e
    if not content:
        raise RuntimeError(f"Groq {model} returned empty content")
    return content


class _RateLimited(Exception):
    pass


async def call_groq(messages: list[dict], model: str = MODEL_FAST, max_tokens: int = 200,
                     temperature: float = 0.3, timeout_seconds: int = 12) -> str:
    """Tries each configured Groq key in round-robin order, moving to the
    next key on a 429 or transient failure. Raises RuntimeError if neither
    key is configured or both fail."""
    keys = _next_key_order()
    if not keys:
        raise RuntimeError("No GROQ_API_KEY_1 / GROQ_API_KEY_2 configured.")

    last_error: Exception | None = None
    for key in keys:
        try:
            return await _call_one(model, messages, max_tokens, temperature, key, timeout_seconds)
        except asyncio.TimeoutError:
            last_error = TimeoutError(f"Groq {model} timed out")
            logger.warning("Groq key timed out on %s, trying next key if any", model)
        except _RateLimited as e:
            last_error = e
            logger.warning("Groq key rate-limited, trying next key if any")
        except Exception as e:
            last_error = e
            logger.warning("Groq call failed (%s), trying next key if any", e)

    raise RuntimeError(f"All Groq keys failed: {last_error}")


def is_configured() -> bool:
    return bool(_get_keys())