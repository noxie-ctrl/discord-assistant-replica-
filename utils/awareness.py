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

Day 4 addition: a server-vibe digest, same shape as the news digest above,
but per-guild instead of global and reading the guild's own recent chat
instead of RSS. Deliberately doesn't fall back to a raw-message dump when
Groq isn't configured (unlike the news digest) — echoing members' actual
messages back into a system prompt with no condensation is a real privacy
smell, whereas raw headlines are public wire copy anyway.
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


# ---------------------------------------------------------------------------
# Server-vibe digest (Day 4) — per-guild, reads the guild's own recent chat
# instead of RSS, distills tone/slang/energy rather than facts.
# ---------------------------------------------------------------------------

SERVER_VIBE_REFRESH_INTERVAL_SECONDS = 6 * 60 * 60  # refresh every 6 hours
SERVER_VIBE_SAMPLE_SIZE = 60  # recent messages sampled per guild

_cached_vibe: dict[int, str] = {}
_cached_vibe_at: dict[int, float] = {}

_VIBE_SYSTEM_PROMPT = (
    "You read a sample of recent Discord messages from one server and describe "
    "the general conversational vibe in 1-2 short sentences: how casual/formal "
    "it is, general energy (chill, chaotic, meme-heavy, supportive, competitive, "
    "etc.), and any recurring slang or in-jokes worth knowing. Do NOT quote "
    "specific messages, do NOT name specific users, and do NOT reference any "
    "specific incident, drama, or sensitive topic mentioned — describe only the "
    "general tone/style, nothing traceable back to one message or person. If "
    "the sample is too thin or mixed to say anything meaningful, output exactly: NONE."
)


async def _condense_vibe(sample_messages: list[str]) -> str:
    if not sample_messages:
        return ""
    # No non-model fallback here on purpose (unlike _condense above) — with
    # no Groq to distill it, the only honest options are silence or echoing
    # members' raw messages into a system prompt, and the latter isn't okay.
    if not groq_client.is_configured():
        return ""

    raw = "\n".join(f"- {m}" for m in sample_messages[-SERVER_VIBE_SAMPLE_SIZE:])
    try:
        return await groq_client.call_groq(
            [
                {"role": "system", "content": _VIBE_SYSTEM_PROMPT},
                {"role": "user", "content": raw},
            ],
            model=groq_client.MODEL_FAST,
            max_tokens=120,
            temperature=0.3,
        )
    except Exception as e:
        logger.warning("Server vibe condensation failed: %s", e)
        return ""


async def refresh_server_vibe(guild_id: int, sample_messages: list[str], force: bool = False) -> str:
    """Condenses a sample of one guild's own recent chat into a short
    tone/vibe descriptor and updates that guild's cache entry. Safe to call
    often — no-ops if that guild's cache is still fresh, unless force=True.
    A thin/mixed sample (model returns NONE) or a failed call leaves the
    previous cached value in place rather than clearing it."""
    now = time.time()
    if not force:
        last_at = _cached_vibe_at.get(guild_id, 0.0)
        if guild_id in _cached_vibe and (now - last_at) < SERVER_VIBE_REFRESH_INTERVAL_SECONDS:
            return _cached_vibe[guild_id]

    try:
        digest = await _condense_vibe(sample_messages)
        if digest and digest.strip().upper() != "NONE":
            _cached_vibe[guild_id] = digest.strip()
            _cached_vibe_at[guild_id] = now
    except Exception as e:
        logger.warning("Server vibe refresh failed for guild %s, keeping previous cache: %s", guild_id, e)

    return _cached_vibe.get(guild_id, "")


def get_cached_server_vibe(guild_id: int) -> str:
    """Non-blocking read for use while building a system prompt — never
    does network/model work itself."""
    return _cached_vibe.get(guild_id, "")