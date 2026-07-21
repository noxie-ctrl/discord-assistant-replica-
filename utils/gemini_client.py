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


# ---------------------------------------------------------------------------
# Embeddings — used by utils/aysa_knowledge.py (book/PDF library ingestion
# + retrieval, stored in Postgres/pgvector). Same OpenAI-compatibility
# layer as call_gemini above, just the /embeddings path instead of
# /chat/completions, so auth/session/error-shape all match.
#
# text-embedding-004 (this file's original model) was fully shut down by
# Google on January 14, 2026 — every call started failing with "models/
# text-embedding-004 is not found for API version v1beta, or is not
# supported for embedContent." This is what caused /aysaseedlibrary to
# report 0 chunks stored / all chunks failed on every source: not a rate
# limit (utils/aysa_knowledge.py's retry-with-backoff correctly didn't
# retry a hard 404-style error), the model itself was gone.
#
# Replacement is gemini-embedding-001, Google's current stable text
# embedding model. Its NATIVE default output is 3072 dimensions, but it
# supports Matryoshka truncation via an explicit dimensions request —
# Google's own docs list 768/1536/3072 as all being good-quality choices,
# so we ask for 768 specifically to keep VECTOR(768) in
# utils/database.py's AYSA_VECTOR_SCHEMA unchanged (no schema migration,
# no re-embedding anything — there was nothing stored yet to re-embed
# anyway, since every prior ingest attempt failed at this exact step).
# Google's docs note that anything short of the full 3072-dim output isn't
# pre-normalized to unit length — cosine distance (what
# db.search_knowledge_chunks' pgvector `<=>` operator computes) is
# scale-invariant so this wouldn't break ranking either way, but we
# normalize explicitly below anyway since it's cheap and it's what Google
# recommends for best quality at a truncated dimension.
#
# If you ever want to move the whole library to 3072-dim for higher
# retrieval quality, that DOES require updating VECTOR(768) to VECTOR(3072)
# in AYSA_VECTOR_SCHEMA and re-ingesting every existing source (the schema
# stores a fixed-width vector column) — not done here to keep this a
# same-day unblock rather than a migration project.
# ---------------------------------------------------------------------------

GEMINI_EMBEDDING_URL = "https://generativelanguage.googleapis.com/v1beta/openai/embeddings"
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 768


def _normalize(vector: list[float]) -> list[float]:
    """L2-normalizes an embedding to unit length — see the module comment
    above on why this matters specifically for a truncated (non-3072)
    gemini-embedding-001 output. A no-op (within floating-point noise) on
    an already-normalized vector, so safe to always apply."""
    norm = sum(v * v for v in vector) ** 0.5
    if norm == 0:
        return vector
    return [v / norm for v in vector]


async def embed_text(text: str, timeout_seconds: int = 15) -> list[float]:
    """Returns a single embedding vector for `text`. Raises RuntimeError on
    any failure — callers (ingestion + retrieval) should treat that as
    'knowledge library temporarily unavailable' rather than crashing the
    whole chat pipeline; see utils/aysa_knowledge.py."""
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set.")

    payload = {"model": EMBEDDING_MODEL, "input": text, "dimensions": EMBEDDING_DIMENSIONS}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    session = await http.get_session()
    async with session.post(GEMINI_EMBEDDING_URL, json=payload, headers=headers, timeout=timeout) as resp:
        if resp.status == 429:
            raise RuntimeError("Gemini embeddings rate-limited (per-project limit).")
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"Gemini embeddings returned {resp.status}: {body[:300]}")
        data = await resp.json()

    try:
        vector = data["data"][0]["embedding"]
    except (KeyError, IndexError) as e:
        raise RuntimeError("Gemini embeddings returned an unexpected response shape") from e
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise RuntimeError(
            f"Gemini embeddings returned {len(vector)} dimensions, expected {EMBEDDING_DIMENSIONS} — "
            "the model may have changed its default output shape again; check EMBEDDING_DIMENSIONS "
            "and AYSA_VECTOR_SCHEMA's VECTOR(768) column together before this silently corrupts search."
        )
    return _normalize(vector)