"""
Cerebras Cloud client — OpenAI-compatible chat completions, same call
shape as groq_client.py and nim_client.py so this project keeps "one way
to call an LLM" even as the number of providers grows.

Three keys (CEREBRAS_API_KEY_1 / _2 / _3) round-robin per call, same
pattern as groq_client.py's _get_keys()/_next_key_order(): a rate limit
on one key rotates to the next instead of failing the whole call.

IMPORTANT — MODEL_CANDIDATES below is UNVERIFIED. Cerebras's public free
model catalog has been reported inconsistently across sources in 2026 —
anywhere from a wide Llama/Qwen3/DeepSeek lineup down to just two models
(gpt-oss-120b + a preview-only model), with the rest reportedly moved
behind paid Dedicated Endpoints. Call get_models() once real keys are
live and confirm what this account can actually reach before trusting
any name here — this is the same lesson NIM's mistral-large-2 silent
deprecation already taught this project once (see the comment on
nim_client.MODEL_CANDIDATES). Don't repeat that mistake by assuming a
blog's model list matches this account's live catalog.
"""

import os
import asyncio
import itertools
import logging

import aiohttp

from utils import http

logger = logging.getLogger("lucy.cerebras_client")

CEREBRAS_API_URL = "https://api.cerebras.ai/v1/chat/completions"
CEREBRAS_MODELS_URL = "https://api.cerebras.ai/v1/models"

# UNVERIFIED against this project's actual account — see module docstring.
# Call get_models() before relying on this in production; update this list
# to match whatever that call actually returns.
MODEL_CANDIDATES = [
    "llama-3.3-70b",
    "llama3.1-8b",
]

_key_cycle = None


def _get_keys() -> list[str]:
    keys = [
        os.getenv("CEREBRAS_API_KEY_1", "").strip(),
        os.getenv("CEREBRAS_API_KEY_2", "").strip(),
        os.getenv("CEREBRAS_API_KEY_3", "").strip(),
    ]
    return [k for k in keys if k]


def _next_key_order() -> list[str]:
    """Returns available keys starting from wherever the round-robin cursor
    currently is, so load spreads across all configured keys over time."""
    global _key_cycle
    keys = _get_keys()
    if not keys:
        return []
    if _key_cycle is None:
        _key_cycle = itertools.cycle(range(len(keys)))
    start = next(_key_cycle)
    return keys[start:] + keys[:start]


class _RateLimited(Exception):
    pass


async def _call_one(model: str, messages: list[dict], max_tokens: int, temperature: float,
                     api_key: str, timeout_seconds: int = 15) -> str:
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
    async with session.post(CEREBRAS_API_URL, json=payload, headers=headers, timeout=timeout) as resp:
        if resp.status == 429:
            raise _RateLimited(f"Cerebras key rate-limited on {model}")
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"Cerebras {model} returned {resp.status}: {body[:300]}")
        data = await resp.json()

    try:
        content = (data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError("Cerebras returned an unexpected response shape") from e
    if not content:
        raise RuntimeError(f"Cerebras {model} returned empty content")
    return content


async def call_cerebras(messages: list[dict], model: str = MODEL_CANDIDATES[0], max_tokens: int = 700,
                         temperature: float = 0.85, timeout_seconds: int = 15) -> str:
    """Tries each configured Cerebras key in round-robin order for the given
    model, moving to the next key on a 429 or transient failure. Raises
    RuntimeError if no key is configured or all keys fail."""
    keys = _next_key_order()
    if not keys:
        raise RuntimeError("No CEREBRAS_API_KEY_1 / CEREBRAS_API_KEY_2 / CEREBRAS_API_KEY_3 configured.")

    last_error: Exception | None = None
    for key in keys:
        try:
            return await _call_one(model, messages, max_tokens, temperature, key, timeout_seconds)
        except asyncio.TimeoutError:
            last_error = TimeoutError(f"Cerebras {model} timed out")
            logger.warning("Cerebras key timed out on %s, trying next key if any", model)
        except _RateLimited as e:
            last_error = e
            logger.warning("Cerebras key rate-limited, trying next key if any")
        except Exception as e:
            last_error = e
            logger.warning("Cerebras call failed (%s), trying next key if any", e)

    raise RuntimeError(f"All Cerebras keys failed: {last_error}")


async def get_models() -> list[str]:
    """Live model list for this account. Call this once keys are set and
    BEFORE trusting MODEL_CANDIDATES above — see module docstring for why.
    Returns model IDs only, uses whichever key is first configured."""
    keys = _get_keys()
    if not keys:
        raise RuntimeError("No CEREBRAS_API_KEY_1 / CEREBRAS_API_KEY_2 / CEREBRAS_API_KEY_3 configured.")
    headers = {"Authorization": f"Bearer {keys[0]}"}
    session = await http.get_session()
    timeout = aiohttp.ClientTimeout(total=10)
    async with session.get(CEREBRAS_MODELS_URL, headers=headers, timeout=timeout) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"Cerebras /v1/models returned {resp.status}: {body[:300]}")
        data = await resp.json()
    return [m.get("id") for m in data.get("data", []) if m.get("id")]


def is_configured() -> bool:
    return bool(_get_keys())