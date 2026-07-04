"""
utils/awareness.py

Gives Lucy passive "peak awareness" of current events without spending a
model call on every message. A background refresh (started from
cogs/ai_chat.py's cog_load) pulls real headlines from the same BBC feeds
cogs/news.py already uses, then condenses them into a short digest string
via Groq (cheap/fast — this is exactly the kind of background task that
shouldn't touch the main NIM quota). The digest is cached in memory and
just re-read (no network/model call) every time a chat reply is built.

If Groq isn't configured, we fall back to a plain title list — still
correct, just less conversationally condensed.
"""

import time
import logging
import xml.etree.ElementTree as ET

import aiohttp

from utils import groq_client

logger = logging.getLogger("lucy.awareness")

REFRESH_INTERVAL_SECONDS = 3 * 60 * 60  # refresh every 3 hours
FEEDS = [
    "http://feeds.bbci.co.uk/news/rss.xml",       # top stories
    "http://feeds.bbci.co.uk/news/world/rss.xml",  # world
]

_cached_digest: str | None = None
_cached_at: float = 0.0


async def _fetch_headlines(limit_per_feed: int = 5) -> list[str]:
    titles: list[str] = []
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in FEEDS:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    raw = await resp.text()
                root = ET.fromstring(raw)
                for item in root.findall(".//item")[:limit_per_feed]:
                    title = (item.findtext("title", default="") or "").strip()
                    if title:
                        titles.append(title)
            except Exception as e:
                logger.warning("Failed fetching feed %s: %s", url, e)
    # de-dupe while preserving order
    seen = set()
    unique = []
    for t in titles:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique[:8]


async def _condense(titles: list[str]) -> str:
    if not titles:
        return ""
    raw_list = "\n".join(f"- {t}" for t in titles)
    if not groq_client.is_configured():
        return raw_list

    try:
        return await groq_client.call_groq(
            [
                {
                    "role": "system",
                    "content": (
                        "Condense these real news headlines into at most 5 short factual "
                        "one-line bullet points a person could casually mention knowing about "
                        "today. No commentary, no markdown headers, just '- fact' lines."
                    ),
                },
                {"role": "user", "content": raw_list},
            ],
            model=groq_client.MODEL_FAST,
            max_tokens=220,
            temperature=0.2,
        )
    except Exception as e:
        logger.warning("News digest condensation failed, using raw titles: %s", e)
        return raw_list


async def refresh_digest(force: bool = False) -> str:
    """Fetches + condenses fresh headlines and updates the cache. Safe to
    call often — internally no-ops if the cache is still fresh, unless
    force=True."""
    global _cached_digest, _cached_at
    now = time.time()
    if not force and _cached_digest is not None and (now - _cached_at) < REFRESH_INTERVAL_SECONDS:
        return _cached_digest

    try:
        titles = await _fetch_headlines()
        digest = await _condense(titles)
        if digest:
            _cached_digest = digest
            _cached_at = now
    except Exception as e:
        logger.warning("News digest refresh failed, keeping previous cache: %s", e)

    return _cached_digest or ""


def get_cached_digest() -> str:
    """Non-blocking read for use while building a system prompt — never
    does network/model work itself."""
    return _cached_digest or ""