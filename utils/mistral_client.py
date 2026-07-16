"""
Mistral AI "La Plateforme" client — native OpenAI-compatible chat
completions, same call shape as every other client in utils/.

Free "Experiment" tier gives rate-limited access to essentially the whole
catalog (Large, Small, Codestral, Pixtral, OCR, embeddings) rather than a
cut-down free-model list — unusual generosity in model *variety* for a
free tier. The actual requests-per-minute number isn't published anymore;
check this account's Admin Console -> Limits page for the real figure
rather than trusting a number from a blog post.
"""

import os
import logging

import aiohttp

from utils import http

logger = logging.getLogger("lucy.mistral_client")

MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

MODEL_CANDIDATES = [
    "mistral-large-latest",
    "mistral-small-latest",
]


def _api_key() -> str:
    return os.getenv("MISTRAL_API_KEY", "").strip()


async def call_mistral(messages: list[dict], model: str = MODEL_CANDIDATES[0], max_tokens: int = 700,
                        temperature: float = 0.85, timeout_seconds: int = 15) -> str:
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY is not set.")

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
    async with session.post(MISTRAL_API_URL, json=payload, headers=headers, timeout=timeout) as resp:
        if resp.status == 429:
            raise RuntimeError(f"Mistral rate-limited on {model}")
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"Mistral {model} returned {resp.status}: {body[:300]}")
        data = await resp.json()

    try:
        content = (data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError("Mistral returned an unexpected response shape") from e
    if not content:
        raise RuntimeError(f"Mistral {model} returned empty content")
    return content


def is_configured() -> bool:
    return bool(_api_key())