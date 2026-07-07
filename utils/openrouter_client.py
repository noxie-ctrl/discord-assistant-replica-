"""
utils/openrouter_client.py

OpenRouter is used for image understanding and as an additional fallback tier
for main chat when both NVIDIA NIM and Groq are unavailable.
"""

import asyncio
import itertools
import logging
import os

import aiohttp

logger = logging.getLogger("lucy.openrouter")

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_CHAT_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini")
DEFAULT_VISION_MODEL = os.getenv("OPENROUTER_VISION_MODEL", "openai/gpt-4.1-mini")

_key_cycle = None


class _RateLimited(Exception):
    pass


def _get_keys() -> list[str]:
    keys = [
        os.getenv("OPENROUTER_API_KEY", "").strip(),
        os.getenv("OPENROUTER_API_KEY_2", "").strip(),
    ]
    return [k for k in keys if k]


def _next_key_order() -> list[str]:
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
        "HTTP-Referer": os.getenv("OPENROUTER_HTTP_REFERER", "https://localhost"),
        "X-Title": os.getenv("OPENROUTER_TITLE", "Lucy Bot"),
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
        content = (data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError("OpenRouter returned an unexpected response shape") from e
    if not content:
        raise RuntimeError(f"OpenRouter {model} returned empty content")
    return content


async def call_openrouter(messages: list[dict], model: str = DEFAULT_CHAT_MODEL,
                          max_tokens: int = 400, temperature: float = 0.3,
                          timeout_seconds: int = 12) -> str:
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


async def describe_images(image_urls: list[str], prompt: str = "Describe the image(s) plainly and briefly.") -> str:
    if not image_urls:
        return ""
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
    return result.strip()


def is_configured() -> bool:
    return bool(_get_keys())
