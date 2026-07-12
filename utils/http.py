"""
Shared aiohttp.ClientSession lifecycle.

Refactor (this session): every external HTTP client in this project —
nim_client, groq_client, openrouter_client, github_client, facts, awareness,
news — was opening a brand new aiohttp.ClientSession on every single call,
including back-to-back calls within the same request (get_weather makes two
GETs that used to be two separate sessions). Each new session pays a fresh
TCP handshake plus TLS negotiation before the actual request even starts.
NIM/Groq/OpenRouter calls in particular happen multiple times per Discord
message (the tool-calling loop, background notes/style-signal passes), so
this was pure latency overhead sitting on the hot path of every message
Lucy responds to.

A single shared, connection-pooled session — created once, reused
everywhere, closed on shutdown — is the standard aiohttp pattern for a
long-running service. Keep-alive connection reuse means most calls to the
same host (api.groq.com, integrate.api.nvidia.com, etc.) skip the
handshake entirely after the first one.
"""

import asyncio
import logging

import aiohttp

logger = logging.getLogger("lucy.http")

_session: aiohttp.ClientSession | None = None
_lock = asyncio.Lock()


async def get_session() -> aiohttp.ClientSession:
    """Returns the shared session, creating it on first use.

    Callers should NOT wrap this in `async with` — that would close the
    shared session for everyone else. Each call site keeps its own
    per-request timeout by passing `timeout=` directly to
    `session.get(...)` / `session.post(...)`; aiohttp applies a per-request
    timeout override cleanly on top of the shared session's connector, so
    each endpoint's existing timeout tuning (8s for facts, longer for NIM's
    first attempt, etc.) is preserved exactly as it was.
    """
    global _session
    if _session is not None and not _session.closed:
        return _session
    async with _lock:
        # Re-check after acquiring the lock — another task may have already
        # created it while we were waiting.
        if _session is None or _session.closed:
            connector = aiohttp.TCPConnector(limit=100, limit_per_host=20, ttl_dns_cache=300)
            _session = aiohttp.ClientSession(connector=connector)
            logger.info("Shared aiohttp session created.")
    return _session


async def close_session() -> None:
    """Call once during bot shutdown (main.py's Lucy.close())."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
        logger.info("Shared aiohttp session closed.")
    _session = None