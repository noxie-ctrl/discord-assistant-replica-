"""
utils/database.py

Postgres-backed persistence layer for Lucy, using Railway's Postgres plugin.

v2 fix: the personality/guild_settings schema now matches the field names
your actual cogs/personality.py and cogs/utility.py use (pronouns, role,
speaking_style, boundaries / chat_trigger_mode) — the first version guessed
wrong names and would have broken /setpersonality and /setchattrigger.
"""

import os
import json
import logging
from datetime import datetime, timezone

import asyncpg

logger = logging.getLogger("lucy.database")

_pool: asyncpg.Pool | None = None

# personality.py does: with open(db.DEFAULT_PERSONALITY_PATH) as f: json.load(f)
DEFAULT_PERSONALITY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "personality_default.json",
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT PRIMARY KEY,
    log_channel_id BIGINT,
    chat_trigger_mode TEXT DEFAULT 'mention',
    chat_channel_id BIGINT,
    welcome_channel_id BIGINT,
    welcome_message TEXT,
    vent_channel_id BIGINT,
    channel_redirection_enabled BOOLEAN DEFAULT TRUE,
    idle_chatter_enabled BOOLEAN DEFAULT TRUE,
    server_vibe_enabled BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS member_events (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    username TEXT,
    event_type TEXT NOT NULL, -- 'join' or 'leave'
    duration_seconds BIGINT,  -- only set on 'leave' — time between this join and leave
    event_time TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_member_events_guild_time
    ON member_events (guild_id, event_time DESC);

CREATE TABLE IF NOT EXISTS personality (
    guild_id BIGINT PRIMARY KEY,
    name TEXT DEFAULT 'Lucy',
    age TEXT DEFAULT '21',
    pronouns TEXT DEFAULT 'she/her',
    role TEXT DEFAULT 'Server admin assistant & friend to everyone here',
    traits TEXT DEFAULT '',
    backstory TEXT DEFAULT '',
    speaking_style TEXT DEFAULT '',
    boundaries TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS warnings (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    moderator_id BIGINT NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_memory (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    speaker_id BIGINT,
    speaker_name TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_profiles (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    username TEXT,
    display_name TEXT,
    message_count INT DEFAULT 0,
    first_seen TIMESTAMPTZ DEFAULT now(),
    last_seen TIMESTAMPTZ DEFAULT now(),
    notes TEXT DEFAULT '',
    relationship_score INT DEFAULT 0,
    preferred_language TEXT,
    response_style TEXT,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS feedback (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_snippet TEXT,
    rating TEXT NOT NULL,
    note TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS game_stats (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    game TEXT NOT NULL,
    wins INT DEFAULT 0,
    losses INT DEFAULT 0,
    draws INT DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, game)
);

CREATE TABLE IF NOT EXISTS economy (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    balance INT DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_memory_guild_channel
    ON chat_memory (guild_id, channel_id, created_at DESC);

CREATE TABLE IF NOT EXISTS image_descriptions (
    cache_key TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Idle chatter fix (this session): idle chatter used to be hardcoded to
-- whatever single channel /setchatchannel pointed at. This is a new table,
-- not a new column, so no MIGRATIONS entry is needed — a guild with no rows
-- here yet just falls back to the old chat_channel_id behavior (see
-- resolve_idle_chatter_channel_ids() in cogs/ai_chat.py).
CREATE TABLE IF NOT EXISTS idle_chatter_channels (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

-- GitHub repo link feature: one row per (guild, repo). last_commit_sha /
-- last_pr_check_at are the polling cursors used by cogs/github.py's
-- background loop to figure out what's new since the previous check.
CREATE TABLE IF NOT EXISTS github_links (
    guild_id BIGINT NOT NULL,
    repo TEXT NOT NULL,               -- "owner/name", lowercase
    channel_id BIGINT NOT NULL,
    added_by BIGINT,
    default_branch TEXT DEFAULT 'main',
    last_commit_sha TEXT,
    last_pr_check_at TIMESTAMPTZ DEFAULT now(),
    notify_commits BOOLEAN DEFAULT TRUE,
    notify_prs BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (guild_id, repo)
);

-- History of everything cogs/github.py has posted (commit batches + PR
-- events), independent of the github_links polling cursors above. This is
-- what powers the weekly digest (aggregate over the last 7 days) and the
-- search_github_activity tool (ai_chat.py answering "what changed in X").
CREATE TABLE IF NOT EXISTS github_activity_log (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    repo TEXT NOT NULL,
    kind TEXT NOT NULL,               -- 'commits' or 'pr'
    ref TEXT,                         -- short sha for commits, PR number (as text) for PRs
    title TEXT NOT NULL,              -- AI summary (commits) or PR title
    detail TEXT,                      -- AI summary (PRs) or raw commit lines
    author TEXT,
    url TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_github_activity_guild_time
    ON github_activity_log (guild_id, created_at DESC);



-- GitHub bot isolation (this session): project-tracking + durable cache
-- for the newly-separated GitHub bot (github_bot.py). Same Postgres
-- instance — no second database, just clearly-prefixed tables.

-- Which repo (if any) a channel/thread is "about" — explicit-set via
-- /projectlink, not passively inferred.
CREATE TABLE IF NOT EXISTS ghbot_projects (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    repo TEXT NOT NULL,
    description TEXT DEFAULT '',
    linked_by BIGINT,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (guild_id, channel_id)
);

-- Durable backstop under cachetools' in-memory cache — survives a Render
-- free-tier restart, which wipes memory but not Postgres.
CREATE TABLE IF NOT EXISTS ghbot_repo_cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    fetched_at TIMESTAMPTZ DEFAULT now()
);

-- The bot's own role/config. Setter command deliberately deferred — the
-- default in code covers the MVP; a /botscope command is a small
-- follow-up once this is live.
CREATE TABLE IF NOT EXISTS ghbot_scope (
    guild_id BIGINT PRIMARY KEY,
    role_description TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT now()
);
"""



# Tables above only get created if they don't exist — they already exist in
# production from the previous deploy, so new columns need explicit ALTERs.
MIGRATIONS = """
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS preferred_language TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS response_style TEXT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS vent_channel_id BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS channel_redirection_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS idle_chatter_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS server_vibe_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS github_digest_channel_id BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS github_last_digest_at TIMESTAMPTZ;
-- Adaptive persona (utils/persona_engine.py): per-user communication-style
-- axes (directness, banter, energy, depth, support_style), stored as plain
-- JSON-encoded TEXT (same pattern as `notes`) rather than JSONB, so no
-- asyncpg codec setup is needed. onboarded_at is set the first time someone
-- completes /vibecheck; NULL just means "never ran it" (passive inference
-- still applies regardless).
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS style_profile TEXT DEFAULT '{}';
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS style_confidence TEXT DEFAULT '{}';
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS onboarded_at TIMESTAMPTZ;
"""


# ---------------------------------------------------------------------------
# Aysa (psychology mentor bot) — isolated tables, aysa_ prefix, same "one
# shared Postgres, clearly-prefixed tables" convention as the GitHub bot's
# ghbot_* tables. Split into two pieces below (core / vector) so a missing
# pgvector extension on the host only disables the knowledge library, not
# the whole bot — see init_pool().
# ---------------------------------------------------------------------------

AYSA_SCHEMA = """
-- One row per person Aysa has ever talked to. `notes` is her rolling,
-- AI-summarized long-term memory of this person (concerns, goals,
-- recurring themes) — same idea as Lucy's user_profiles.notes, kept in
-- Aysa's own table since the two bots deliberately don't share a memory
-- space or a persona.
CREATE TABLE IF NOT EXISTS aysa_students (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    display_name TEXT,
    message_count INT DEFAULT 0,
    notes TEXT DEFAULT '',
    first_seen TIMESTAMPTZ DEFAULT now(),
    last_seen TIMESTAMPTZ DEFAULT now()
);

-- Full mentoring conversation log, per user rather than per guild/channel —
-- Aysa's relationship is with the person, whether they reach her in a DM
-- or an @mention in a server channel. Kept to a rolling window in code
-- (see add_conversation_message), with aysa_students.notes carrying
-- continuity across the cap, same split as Lucy's chat_memory + notes.
CREATE TABLE IF NOT EXISTS aysa_conversations (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    role TEXT NOT NULL,             -- 'user' or 'assistant'
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_aysa_conversations_user_time
    ON aysa_conversations (user_id, created_at DESC);

-- A course is a named curriculum; lessons belong to it in order.
CREATE TABLE IF NOT EXISTS aysa_courses (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_by BIGINT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- video_url/paper_url are real sourced links (utils/aysa_content.py
-- searches YouTube + Semantic Scholar when a lesson is added); summary and
-- comprehension_questions are Aysa's own AI-generated material built from
-- those sources — what she actually teaches from, day to day.
CREATE TABLE IF NOT EXISTS aysa_lessons (
    id SERIAL PRIMARY KEY,
    course_id INT NOT NULL REFERENCES aysa_courses(id) ON DELETE CASCADE,
    order_index INT NOT NULL,
    topic TEXT NOT NULL,
    video_url TEXT,
    video_title TEXT,
    paper_url TEXT,
    paper_title TEXT,
    summary TEXT,
    comprehension_questions TEXT,   -- JSON-encoded list[str]
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (course_id, order_index)
);

-- One row per (student, course). current_lesson_index is the lesson
-- they're currently ON — only advances once that lesson's comprehension
-- discussion is marked done (see aysa_lesson_progress + cogs/aysa_courses.py).
CREATE TABLE IF NOT EXISTS aysa_enrollments (
    user_id BIGINT NOT NULL,
    course_id INT NOT NULL REFERENCES aysa_courses(id) ON DELETE CASCADE,
    current_lesson_index INT NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'completed' | 'paused'
    enrolled_at TIMESTAMPTZ DEFAULT now(),
    last_activity_at TIMESTAMPTZ DEFAULT now(),
    last_nudged_at TIMESTAMPTZ,
    PRIMARY KEY (user_id, course_id)
);

-- Per-lesson progress: when a student marked it watched, and whether
-- Aysa's follow-up comprehension chat (her "how'd it land, what did you
-- think" check) has happened yet.
CREATE TABLE IF NOT EXISTS aysa_lesson_progress (
    user_id BIGINT NOT NULL,
    course_id INT NOT NULL,
    lesson_index INT NOT NULL,
    watched_at TIMESTAMPTZ,
    discussed_at TIMESTAMPTZ,
    comprehension_notes TEXT DEFAULT '',
    PRIMARY KEY (user_id, course_id, lesson_index)
);
"""

AYSA_MIGRATIONS = """
ALTER TABLE aysa_enrollments ADD COLUMN IF NOT EXISTS last_nudged_at TIMESTAMPTZ;
ALTER TABLE aysa_lesson_progress ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;
"""

# Split out from AYSA_SCHEMA: requires the pgvector extension, which isn't
# guaranteed to be available/permitted on every Postgres host. Executed
# separately in init_pool() inside a try/except — on failure the knowledge
# library (book/PDF search) just stays disabled, same "additive, not a hard
# dependency" pattern as GITHUB_BOT_TOKEN being unset.
AYSA_VECTOR_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

-- Books/papers/PDFs an admin has fed Aysa for her general knowledge
-- library (distinct from a course's per-lesson sources above).
CREATE TABLE IF NOT EXISTS aysa_knowledge_sources (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    added_by BIGINT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 768 dims matches Gemini's text-embedding-004 (see
-- utils/gemini_client.embed_text) — change both together if the embedding
-- model ever changes.
CREATE TABLE IF NOT EXISTS aysa_knowledge_chunks (
    id SERIAL PRIMARY KEY,
    source_id INT NOT NULL REFERENCES aysa_knowledge_sources(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,
    embedding VECTOR(768),
    created_at TIMESTAMPTZ DEFAULT now()
);
"""

# Set at the end of init_pool() — cogs/aysa_chat.py checks this before
# registering the knowledge-search tool at all, so the model never even
# sees a tool that would just error every time.
KNOWLEDGE_LIBRARY_AVAILABLE = False


async def init_pool():
    """Called once explicitly in main.py before either bot starts (this
    session, multi-bot support) — idempotent, so Lucy.setup_hook()'s
    existing call to this is now just a harmless no-op instead of racing
    to create a second pool."""
    global _pool
    if _pool is not None:
        return _pool
    dsn = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. In Railway, add a variable reference "
            "to your Postgres service (e.g. ${{Postgres.DATABASE_URL}}) on "
            "the worker service."
        )
    if dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql://", 1)

    # Portability fix: Render/Koyeb (and some other hosts) require SSL for
    # their managed Postgres, but Railway's internal Postgres does not. Rather
    # than hardcoding per-platform behavior, we let asyncpg auto-negotiate:
    # passing ssl="require" when the DSN doesn't already specify it covers the
    # common case where a host enforces SSL but the connection string omits
    # the parameter. If sslmode is already in the DSN, asyncpg respects it.
    # This is a no-op on Railway (same connection either way) and required on
    # Render/Koyeb free-tier Postgres.
    connect_kwargs = {"min_size": 1, "max_size": 10}
    if "sslmode=" not in dsn and "?ssl=" not in dsn:
        connect_kwargs["ssl"] = "require"

    _pool = await asyncpg.create_pool(dsn=dsn, **connect_kwargs)
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)
        await conn.execute(MIGRATIONS)
        await conn.execute(AYSA_SCHEMA)
        await conn.execute(AYSA_MIGRATIONS)

        global KNOWLEDGE_LIBRARY_AVAILABLE
        try:
            await conn.execute(AYSA_VECTOR_SCHEMA)
            KNOWLEDGE_LIBRARY_AVAILABLE = True
        except Exception:
            logger.warning(
                "pgvector unavailable on this Postgres host — Aysa's book/PDF "
                "knowledge library will stay disabled. Everything else (chat, "
                "memory, courses) is unaffected.", exc_info=True,
            )
            KNOWLEDGE_LIBRARY_AVAILABLE = False
    logger.info("Database pool initialized, schema ensured, migrations applied.")
    return _pool

DEFAULT_GHBOT_ROLE = (
    "You are a focused GitHub/project-tracking assistant for a team working on "
    "shared coding projects in this Discord server. You help with repo status, "
    "code questions, and what's happening on a given project. You are not Lucy "
    "and don't share her persona — keep responses plain, technical, and brief."
)


async def set_project_link(guild_id: int, channel_id: int, repo: str, description: str, linked_by: int):
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ghbot_projects (guild_id, channel_id, repo, description, linked_by)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (guild_id, channel_id) DO UPDATE
                SET repo = EXCLUDED.repo, description = EXCLUDED.description, linked_by = EXCLUDED.linked_by
            """,
            guild_id, channel_id, repo, description, linked_by,
        )


async def get_project_link(guild_id: int, channel_id: int) -> dict | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ghbot_projects WHERE guild_id = $1 AND channel_id = $2",
            guild_id, channel_id,
        )
        return dict(row) if row else None


async def remove_project_link(guild_id: int, channel_id: int) -> bool:
    pool = _require_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM ghbot_projects WHERE guild_id = $1 AND channel_id = $2",
            guild_id, channel_id,
        )
        return result.endswith(" 1")


async def get_repo_cache(cache_key: str, max_age_seconds: int) -> str | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payload, fetched_at FROM ghbot_repo_cache WHERE cache_key = $1", cache_key
        )
    if row is None:
        return None
    age = (datetime.now(timezone.utc) - row["fetched_at"]).total_seconds()
    if age > max_age_seconds:
        return None
    return row["payload"]


async def set_repo_cache(cache_key: str, payload: str):
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ghbot_repo_cache (cache_key, payload, fetched_at)
            VALUES ($1, $2, now())
            ON CONFLICT (cache_key) DO UPDATE SET payload = EXCLUDED.payload, fetched_at = now()
            """,
            cache_key, payload,
        )


async def get_ghbot_scope(guild_id: int) -> str:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT role_description FROM ghbot_scope WHERE guild_id = $1", guild_id
        )
    return row["role_description"] if row and row["role_description"] else DEFAULT_GHBOT_ROLE


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def _require_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized — call init_pool() first.")
    return _pool


# ---------------------------------------------------------------------------
# Guild settings
# ---------------------------------------------------------------------------

async def get_guild_settings(guild_id: int) -> dict:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM guild_settings WHERE guild_id = $1", guild_id
        )
        if row is None:
            await conn.execute(
                "INSERT INTO guild_settings (guild_id) VALUES ($1) "
                "ON CONFLICT (guild_id) DO NOTHING",
                guild_id,
            )
            row = await conn.fetchrow(
                "SELECT * FROM guild_settings WHERE guild_id = $1", guild_id
            )
        return dict(row)


VALID_GUILD_SETTING_FIELDS = {
    "log_channel_id", "chat_trigger_mode", "chat_channel_id", "welcome_channel_id",
    "welcome_message", "vent_channel_id", "channel_redirection_enabled",
    "idle_chatter_enabled", "server_vibe_enabled", "github_digest_channel_id",
    "github_last_digest_at",
}


async def update_guild_setting(guild_id: int, **kwargs):
    """update_guild_setting(guild_id, chat_trigger_mode='mention', chat_channel_id=123)"""
    if not kwargs:
        return
    # Hardening fix (this session): every call site today passes literal,
    # hardcoded kwarg names written in this codebase (see cogs/utility.py,
    # cogs/github.py), so this wasn't reachable with attacker-controlled
    # column names in practice — but unlike set_personality_field just
    # below, which validates against VALID_PERSONALITY_FIELDS, this had NO
    # guard at all before the column name landed in an f-string. That's a
    # foot-gun waiting for a future refactor (e.g. a generic "/setconfig
    # <key> <value>" command mapping free text into these kwargs) to turn
    # into a real SQL injection with nobody noticing until it's exploited.
    # Whitelisting now costs nothing for legitimate callers and closes that
    # off permanently.
    unknown = set(kwargs) - VALID_GUILD_SETTING_FIELDS
    if unknown:
        raise ValueError(f"Unknown guild_settings field(s): {', '.join(sorted(unknown))}")
    await get_guild_settings(guild_id)  # ensure row exists
    pool = _require_pool()
    columns = list(kwargs.keys())
    set_clause = ", ".join(f"{col} = ${i+2}" for i, col in enumerate(columns))
    values = [kwargs[col] for col in columns]
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE guild_settings SET {set_clause} WHERE guild_id = $1",
            guild_id, *values,
        )


# ---------------------------------------------------------------------------
# Idle chatter channels (multi-channel, this session's fix)
# ---------------------------------------------------------------------------

async def add_idle_chatter_channel(guild_id: int, channel_id: int):
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO idle_chatter_channels (guild_id, channel_id) VALUES ($1, $2) "
            "ON CONFLICT (guild_id, channel_id) DO NOTHING",
            guild_id, channel_id,
        )


async def remove_idle_chatter_channel(guild_id: int, channel_id: int):
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM idle_chatter_channels WHERE guild_id = $1 AND channel_id = $2",
            guild_id, channel_id,
        )


async def get_idle_chatter_channels(guild_id: int) -> list[int]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT channel_id FROM idle_chatter_channels WHERE guild_id = $1",
            guild_id,
        )
        return [r["channel_id"] for r in rows]


# ---------------------------------------------------------------------------
# Personality
# ---------------------------------------------------------------------------

def _load_default_personality() -> dict:
    """personality_default.json is meant to be the single source of truth for
    a fresh personality. Previously new rows were seeded from this table's
    bare SQL column DEFAULTs instead (which don't match the JSON file, and
    silently drift from it), so a brand-new guild's very first message used a
    different, thinner personality than /resetpersonality would give it.
    This makes both paths read from the same file."""
    import json
    try:
        with open(DEFAULT_PERSONALITY_PATH, "r") as f:
            return json.load(f)
    except Exception:
        logger.warning("Could not load personality_default.json, falling back to bare row.")
        return {}


async def get_personality(guild_id: int) -> dict:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM personality WHERE guild_id = $1", guild_id
        )
        if row is None:
            defaults = _load_default_personality()
            await conn.execute(
                """
                INSERT INTO personality (guild_id, name, age, pronouns, role, traits, backstory, speaking_style, boundaries)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (guild_id) DO NOTHING
                """,
                guild_id,
                defaults.get("name", "Lucy"),
                defaults.get("age", "21"),
                defaults.get("pronouns", "she/her"),
                defaults.get("role", "Server admin assistant & friend to everyone here"),
                defaults.get("traits", ""),
                defaults.get("backstory", ""),
                defaults.get("speaking_style", ""),
                defaults.get("boundaries", ""),
            )
            row = await conn.fetchrow(
                "SELECT * FROM personality WHERE guild_id = $1", guild_id
            )
        return dict(row)


VALID_PERSONALITY_FIELDS = {
    "name", "age", "pronouns", "role", "traits", "backstory", "speaking_style", "boundaries"
}


async def set_personality_field(guild_id: int, field: str, value: str):
    if field not in VALID_PERSONALITY_FIELDS:
        raise ValueError(f"Unknown personality field: {field}")
    await get_personality(guild_id)  # ensure row exists
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE personality SET {field} = $2 WHERE guild_id = $1",
            guild_id, value,
        )


async def reset_personality(guild_id: int):
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM personality WHERE guild_id = $1", guild_id)
    await get_personality(guild_id)


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------

async def add_warning(guild_id: int, user_id: int, moderator_id: int, reason: str):
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason) "
            "VALUES ($1, $2, $3, $4)",
            guild_id, user_id, moderator_id, reason,
        )


async def get_warnings(guild_id: int, user_id: int) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM warnings WHERE guild_id = $1 AND user_id = $2 "
            "ORDER BY created_at DESC",
            guild_id, user_id,
        )
        return [dict(r) for r in rows]


async def clear_warnings(guild_id: int, user_id: int):
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM warnings WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )


# ---------------------------------------------------------------------------
# Chat memory (short-term, per channel)
# ---------------------------------------------------------------------------

async def add_chat_message(guild_id: int, channel_id: int, speaker_id: int | None,
                             speaker_name: str | None, role: str, content: str):
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO chat_memory (guild_id, channel_id, speaker_id, speaker_name, role, content) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            guild_id, channel_id, speaker_id, speaker_name, role, content,
        )
        await conn.execute(
            """
            DELETE FROM chat_memory
            WHERE id IN (
                SELECT id FROM chat_memory
                WHERE guild_id = $1 AND channel_id = $2
                ORDER BY created_at DESC
                OFFSET 24
            )
            """,
            guild_id, channel_id,
        )


async def get_chat_history(guild_id: int, channel_id: int, limit: int = 24) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM chat_memory WHERE guild_id = $1 AND channel_id = $2 "
            "ORDER BY created_at ASC LIMIT $3",
            guild_id, channel_id, limit,
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# User profiles (long-term memory per user, per guild)
# ---------------------------------------------------------------------------

async def get_profile(guild_id: int, user_id: int) -> dict | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM user_profiles WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )
        return dict(row) if row else None


async def touch_profile(guild_id: int, user_id: int, username: str, display_name: str) -> dict:
    """Upsert a profile, bump message_count + last_seen + relationship_score.
    Stores the immutable Discord username alongside the (changeable) display
    name and the id, so long-term memory is keyed on something more durable
    than a nickname. Returns the fresh row.

    relationship_score climbs by 1 per message (see get_relationship_tier
    below for the tier thresholds) — this is what lets Lucy naturally warm
    up to someone over time instead of treating every conversation like the
    first one."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO user_profiles (guild_id, user_id, username, display_name, message_count, last_seen, relationship_score)
            VALUES ($1, $2, $3, $4, 1, now(), 1)
            ON CONFLICT (guild_id, user_id) DO UPDATE
            SET username = $3,
                display_name = $4,
                message_count = user_profiles.message_count + 1,
                last_seen = now(),
                relationship_score = user_profiles.relationship_score + 1
            RETURNING *
            """,
            guild_id, user_id, username, display_name,
        )
        return dict(row)


# Tier thresholds on relationship_score (roughly: score climbs ~1/message,
# plus small bumps from positive feedback and playing games together — see
# add_feedback callers and record_game_result callers). Tuned so a
# reasonably active member reaches "friend" within a few real conversations,
# not months.
RELATIONSHIP_TIERS = [
    (250, "best friend"),
    (80, "close friend"),
    (20, "friend"),
    (0, "acquaintance"),
]


def get_relationship_tier(score: int) -> str:
    for threshold, label in RELATIONSHIP_TIERS:
        if score >= threshold:
            return label
    return "acquaintance"


async def set_user_preference(guild_id: int, user_id: int, *, preferred_language: str | None = None,
                                response_style: str | None = None):
    updates = {}
    if preferred_language is not None:
        updates["preferred_language"] = preferred_language
    if response_style is not None:
        updates["response_style"] = response_style
    if not updates:
        return
    pool = _require_pool()
    columns = list(updates.keys())
    set_clause = ", ".join(f"{col} = ${i+3}" for i, col in enumerate(columns))
    values = [updates[col] for col in columns]
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE user_profiles SET {set_clause} WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, *values,
        )


async def update_profile_notes(guild_id: int, user_id: int, notes: str):
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_profiles SET notes = $3 WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, notes,
        )


async def save_style_profile(guild_id: int, user_id: int, profile: dict, confidence: dict):
    """Persists the (style_profile, style_confidence) dicts produced by
    utils/persona_engine.py's apply_*_deltas functions. Callers pass plain
    dicts; this is the only place that touches JSON encoding for them."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_profiles SET style_profile = $3, style_confidence = $4 "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, json.dumps(profile or {}), json.dumps(confidence or {}),
        )


async def mark_onboarded(guild_id: int, user_id: int):
    """Called once /vibecheck finishes (cogs/preferences.py). Purely
    informational — doesn't gate passive adaptation, just lets the
    first-message nudge in cogs/ai_chat.py know not to offer it again."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_profiles SET onboarded_at = now() WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )


async def adjust_relationship_score(guild_id: int, user_id: int, delta: int):
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_profiles SET relationship_score = GREATEST(relationship_score + $3, 0) "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, delta,
        )


# ---------------------------------------------------------------------------
# Mini-game stats
# ---------------------------------------------------------------------------

async def record_game_result(guild_id: int, user_id: int, game: str, result: str):
    """result is 'win' | 'loss' | 'draw'. Playing together (any result) is a
    small bonding signal, so it nudges relationship_score a little — same
    idea as the feedback bump above, just smaller and unconditional."""
    col = {"win": "wins", "loss": "losses", "draw": "draws"}.get(result)
    if col is None:
        raise ValueError("result must be 'win', 'loss', or 'draw'")
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO game_stats (guild_id, user_id, game, {col})
            VALUES ($1, $2, $3, 1)
            ON CONFLICT (guild_id, user_id, game) DO UPDATE
            SET {col} = game_stats.{col} + 1
            """,
            guild_id, user_id, game,
        )
    if user_id != _BOT_PLACEHOLDER_ID:
        await adjust_relationship_score(guild_id, user_id, 1)


# record_game_result is also called with the bot's own user id (Lucy "vs AI"
# guess-the-number wins) — skip the relationship bump in that one case since
# there's no user_profiles row for the bot and it wouldn't mean anything.
_BOT_PLACEHOLDER_ID = None  # set at runtime by main.py via set_bot_user_id()


def set_bot_user_id(bot_user_id: int):
    global _BOT_PLACEHOLDER_ID
    _BOT_PLACEHOLDER_ID = bot_user_id


async def get_game_stats(guild_id: int, user_id: int, game: str) -> dict:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM game_stats WHERE guild_id = $1 AND user_id = $2 AND game = $3",
            guild_id, user_id, game,
        )
        return dict(row) if row else {"wins": 0, "losses": 0, "draws": 0}


# Trivia difficulty progression — driven entirely by the existing game_stats
# "wins" count for game='trivia' (already incremented on every correct answer
# via record_game_result), so no schema migration needed. Thresholds tuned so
# progression feels earned but reachable within a normal play session or two,
# not something that takes weeks.
TRIVIA_LEVEL_THRESHOLDS = [
    (30, "hard"),
    (10, "medium"),
    (0, "easy"),
]


def get_trivia_level(correct_count: int) -> str:
    for threshold, label in TRIVIA_LEVEL_THRESHOLDS:
        if correct_count >= threshold:
            return label
    return "easy"


def trivia_next_level_info(correct_count: int) -> tuple[str | None, int]:
    """Returns (next_level_name, correct_answers_still_needed), or (None, 0)
    if already at the top tier."""
    for threshold, label in reversed(TRIVIA_LEVEL_THRESHOLDS):
        if correct_count < threshold:
            return label, threshold - correct_count
    return None, 0


# ---------------------------------------------------------------------------
# Cross-channel continuity — so switching channels doesn't reset context
# ---------------------------------------------------------------------------

async def get_recent_messages_by_user(guild_id: int, user_id: int, limit: int = 10) -> list[dict]:
    """A user's own recent messages across ANY channel in the guild, most
    recent last. Used to give Lucy continuity when someone follows up with
    her in a different channel than where the conversation started."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM chat_memory
            WHERE guild_id = $1 AND speaker_id = $2 AND role = 'user'
            ORDER BY created_at DESC
            LIMIT $3
            """,
            guild_id, user_id, limit,
        )
        return [dict(r) for r in reversed(rows)]


async def get_recent_guild_messages(guild_id: int, limit: int = 60) -> list[dict]:
    """Recent user messages across ALL channels in a guild, most recent
    last. Used only to build the sample for the server-vibe digest
    (utils/awareness.py) — not tied to any one user or channel."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM chat_memory
            WHERE guild_id = $1 AND role = 'user'
            ORDER BY created_at DESC
            LIMIT $2
            """,
            guild_id, limit,
        )
        return [dict(r) for r in reversed(rows)]


# ---------------------------------------------------------------------------
# Feedback (reaction-based, feeds the model rather than "training" it)
# ---------------------------------------------------------------------------

async def add_feedback(guild_id: int, user_id: int, channel_id: int,
                         message_snippet: str, rating: str, note: str | None = None):
    """rating is 'up' or 'down'. A 👍 is a small direct signal the
    conversation actually went well, so it nudges relationship_score a bit
    beyond the flat per-message bump; a 👎 nudges it back down slightly."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO feedback (guild_id, user_id, channel_id, message_snippet, rating, note) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            guild_id, user_id, channel_id, message_snippet, rating, note,
        )
    await adjust_relationship_score(guild_id, user_id, 3 if rating == "up" else -1)


async def get_recent_negative_feedback(guild_id: int, limit: int = 5) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM feedback WHERE guild_id = $1 AND rating = 'down' "
            "ORDER BY created_at DESC LIMIT $2",
            guild_id, limit,
        )
        return [dict(r) for r in rows]


async def get_feedback_summary(guild_id: int) -> dict:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT "
            "COUNT(*) FILTER (WHERE rating = 'up') AS up, "
            "COUNT(*) FILTER (WHERE rating = 'down') AS down "
            "FROM feedback WHERE guild_id = $1",
            guild_id,
        )
        return dict(row)


# ---------------------------------------------------------------------------
# Economy (shared currency across all mini-games)
# ---------------------------------------------------------------------------

async def get_balance(guild_id: int, user_id: int) -> int:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT balance FROM economy WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )
        return row["balance"] if row else 0


async def adjust_balance(guild_id: int, user_id: int, delta: int) -> int:
    """Positive delta credits, negative debits. Returns the new balance."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO economy (guild_id, user_id, balance)
            VALUES ($1, $2, GREATEST($3, 0))
            ON CONFLICT (guild_id, user_id) DO UPDATE
            SET balance = GREATEST(economy.balance + $3, 0)
            RETURNING balance
            """,
            guild_id, user_id, delta,
        )
        return row["balance"]


async def get_leaderboard(guild_id: int, limit: int = 10) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM economy WHERE guild_id = $1 ORDER BY balance DESC LIMIT $2",
            guild_id, limit,
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Member join/leave log
# ---------------------------------------------------------------------------

async def set_vent_channel(guild_id: int, channel_id: int):
    await update_guild_setting(guild_id, vent_channel_id=channel_id)


async def record_member_event(guild_id: int, user_id: int, username: str, event_type: str,
                                 duration_seconds: int | None = None):
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO member_events (guild_id, user_id, username, event_type, duration_seconds) "
            "VALUES ($1, $2, $3, $4, $5)",
            guild_id, user_id, username, event_type, duration_seconds,
        )


async def get_recent_member_events(guild_id: int, limit: int = 15) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM member_events WHERE guild_id = $1 ORDER BY event_time DESC LIMIT $2",
            guild_id, limit,
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Image description cache
# ---------------------------------------------------------------------------

async def get_image_description(cache_key: str) -> str | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT description FROM image_descriptions WHERE cache_key = $1",
            cache_key,
        )
        return row["description"] if row else None


async def set_image_description(cache_key: str, description: str):
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO image_descriptions (cache_key, description) VALUES ($1, $2) "
            "ON CONFLICT (cache_key) DO UPDATE SET description = EXCLUDED.description, created_at = now()",
            cache_key,
            description,
        )


def _normalize_cache_key(key: str) -> str:
    """Internal normalizer for cache keys; kept small for future uses."""
    return key.strip()


# ---------------------------------------------------------------------------
# GitHub repo links
# ---------------------------------------------------------------------------

async def add_github_link(guild_id: int, repo: str, channel_id: int, added_by: int,
                            default_branch: str, last_commit_sha: str | None) -> None:
    """repo is the normalized 'owner/name' string. On conflict (repo already
    linked in this guild), re-points it at the new channel and resets the
    polling cursors so the next cycle establishes a fresh baseline instead
    of dumping old history into the new channel."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO github_links
                (guild_id, repo, channel_id, added_by, default_branch, last_commit_sha, last_pr_check_at)
            VALUES ($1, $2, $3, $4, $5, $6, now())
            ON CONFLICT (guild_id, repo) DO UPDATE
            SET channel_id = EXCLUDED.channel_id,
                added_by = EXCLUDED.added_by,
                default_branch = EXCLUDED.default_branch,
                last_commit_sha = EXCLUDED.last_commit_sha,
                last_pr_check_at = now()
            """,
            guild_id, repo, channel_id, added_by, default_branch, last_commit_sha,
        )


async def remove_github_link(guild_id: int, repo: str) -> bool:
    """Returns True if a row was actually deleted."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM github_links WHERE guild_id = $1 AND repo = $2",
            guild_id, repo,
        )
        return result.endswith(" 1")


async def list_github_links(guild_id: int) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM github_links WHERE guild_id = $1 ORDER BY repo", guild_id
        )
        return [dict(r) for r in rows]


async def get_all_github_links() -> list[dict]:
    """Every linked repo across every guild — used by the background
    polling loop, which then checks each guild's channel still exists."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM github_links")
        return [dict(r) for r in rows]


async def update_github_link_state(guild_id: int, repo: str, *, last_commit_sha: str | None = None,
                                     last_pr_check_at=None) -> None:
    """Partial update of the polling cursors after a background check.
    Only the fields passed are updated."""
    updates = {}
    if last_commit_sha is not None:
        updates["last_commit_sha"] = last_commit_sha
    if last_pr_check_at is not None:
        updates["last_pr_check_at"] = last_pr_check_at
    if not updates:
        return

    pool = _require_pool()
    columns = list(updates.keys())
    set_clause = ", ".join(f"{col} = ${i + 3}" for i, col in enumerate(columns))
    query = f"UPDATE github_links SET {set_clause} WHERE guild_id = $1 AND repo = $2"
    async with pool.acquire() as conn:
        await conn.execute(query, guild_id, repo, *updates.values())


async def log_github_activity(guild_id: int, repo: str, kind: str, ref: str | None, title: str,
                                 detail: str | None = None, author: str | None = None,
                                 url: str | None = None) -> None:
    """kind is 'commits' or 'pr'. Called every time cogs/github.py posts an
    update, independent of the github_links polling cursors — this is a
    durable log read by the weekly digest and the search_github_activity
    tool, not a cursor that gets overwritten."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO github_activity_log (guild_id, repo, kind, ref, title, detail, author, url) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            guild_id, repo, kind, ref, title, detail, author, url,
        )


async def get_recent_github_activity(guild_id: int, repo: str | None = None, days: int = 7,
                                        limit: int = 30) -> list[dict]:
    """Used by the search_github_activity tool (ai_chat.py) — recent
    commit/PR activity, optionally filtered to one repo, most recent
    first."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        if repo:
            rows = await conn.fetch(
                """
                SELECT * FROM github_activity_log
                WHERE guild_id = $1 AND repo = $2 AND created_at > now() - ($3 || ' days')::interval
                ORDER BY created_at DESC LIMIT $4
                """,
                guild_id, repo, str(days), limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT * FROM github_activity_log
                WHERE guild_id = $1 AND created_at > now() - ($2 || ' days')::interval
                ORDER BY created_at DESC LIMIT $3
                """,
                guild_id, str(days), limit,
            )
        return [dict(r) for r in rows]


async def get_guilds_due_for_digest() -> list[dict]:
    """Guilds with a github_digest_channel_id set whose last digest was
    sent more than 6 days ago (or never) — the daily digest-check loop
    calls this and only actually posts on the configured weekday, but this
    keeps the SQL side of the "has it been a week yet" logic in one place."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM guild_settings
            WHERE github_digest_channel_id IS NOT NULL
              AND (github_last_digest_at IS NULL OR github_last_digest_at < now() - interval '6 days')
            """
        )
        return [dict(r) for r in rows]


async def mark_digest_sent(guild_id: int) -> None:
    await update_guild_setting(guild_id, github_last_digest_at=datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Aysa (psychology mentor bot) — students & rolling memory
# ---------------------------------------------------------------------------

AYSA_CONVERSATION_WINDOW = 60  # rows kept per user in aysa_conversations


async def touch_student(user_id: int, username: str, display_name: str) -> dict:
    """Upsert a student row, bump message_count + last_seen. Same shape as
    Lucy's touch_profile, minus relationship_score/guild scoping — Aysa
    isn't guild-scoped, and 'warming up' isn't the framing here."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO aysa_students (user_id, username, display_name, message_count, last_seen)
            VALUES ($1, $2, $3, 1, now())
            ON CONFLICT (user_id) DO UPDATE
            SET username = $2,
                display_name = $3,
                message_count = aysa_students.message_count + 1,
                last_seen = now()
            RETURNING *
            """,
            user_id, username, display_name,
        )
        return dict(row)


async def get_student(user_id: int) -> dict | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM aysa_students WHERE user_id = $1", user_id)
        return dict(row) if row else None


async def update_student_notes(user_id: int, notes: str) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE aysa_students SET notes = $2 WHERE user_id = $1", user_id, notes
        )


async def add_conversation_message(user_id: int, role: str, content: str) -> None:
    """Logs one turn and prunes to AYSA_CONVERSATION_WINDOW rows for this
    user — same rolling-window + offset-delete pattern as Lucy's
    add_chat_message, just keyed on user_id instead of guild/channel since
    this is a 1:1 relationship regardless of where the message came in."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO aysa_conversations (user_id, role, content) VALUES ($1, $2, $3)",
            user_id, role, content,
        )
        await conn.execute(
            """
            DELETE FROM aysa_conversations
            WHERE id IN (
                SELECT id FROM aysa_conversations
                WHERE user_id = $1
                ORDER BY created_at DESC
                OFFSET $2
            )
            """,
            user_id, AYSA_CONVERSATION_WINDOW,
        )


async def get_conversation_history(user_id: int, limit: int = AYSA_CONVERSATION_WINDOW) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM aysa_conversations WHERE user_id = $1 ORDER BY created_at ASC LIMIT $2",
            user_id, limit,
        )
        return [dict(r) for r in rows]


async def clear_conversation(user_id: int) -> None:
    """Backs a /aysaforget-style privacy command — wipes the transcript AND
    the rolling notes summary, a full reset rather than a partial one."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM aysa_conversations WHERE user_id = $1", user_id)
        await conn.execute("UPDATE aysa_students SET notes = '' WHERE user_id = $1", user_id)


# ---------------------------------------------------------------------------
# Aysa — courses & lessons
# ---------------------------------------------------------------------------

async def create_course(title: str, description: str, created_by: int) -> dict:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO aysa_courses (title, description, created_by) VALUES ($1, $2, $3) RETURNING *",
            title, description, created_by,
        )
        return dict(row)


async def list_courses() -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM aysa_courses ORDER BY title")
        return [dict(r) for r in rows]


async def get_course(course_id: int) -> dict | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM aysa_courses WHERE id = $1", course_id)
        return dict(row) if row else None


async def find_course_by_title(title: str) -> dict | None:
    """Case-insensitive lookup so /aysaenroll works with however the user
    capitalized the course name."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM aysa_courses WHERE lower(title) = lower($1)", title
        )
        return dict(row) if row else None


async def add_lesson(course_id: int, order_index: int, topic: str, *, video_url: str | None = None,
                       video_title: str | None = None, paper_url: str | None = None,
                       paper_title: str | None = None, summary: str | None = None,
                       comprehension_questions: str | None = None) -> dict:
    """comprehension_questions is a JSON-encoded list[str] — see
    utils/aysa_content.py, which builds summary + comprehension_questions
    together from the sourced video/paper."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO aysa_lessons
                (course_id, order_index, topic, video_url, video_title, paper_url, paper_title,
                 summary, comprehension_questions)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (course_id, order_index) DO UPDATE
            SET topic = $3, video_url = $4, video_title = $5, paper_url = $6, paper_title = $7,
                summary = $8, comprehension_questions = $9
            RETURNING *
            """,
            course_id, order_index, topic, video_url, video_title, paper_url, paper_title,
            summary, comprehension_questions,
        )
        return dict(row)


async def get_lesson(course_id: int, order_index: int) -> dict | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM aysa_lessons WHERE course_id = $1 AND order_index = $2",
            course_id, order_index,
        )
        return dict(row) if row else None


async def get_lessons_for_course(course_id: int) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM aysa_lessons WHERE course_id = $1 ORDER BY order_index", course_id
        )
        return [dict(r) for r in rows]


async def count_lessons(course_id: int) -> int:
    pool = _require_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM aysa_lessons WHERE course_id = $1", course_id
        )


# ---------------------------------------------------------------------------
# Aysa — enrollments & progress
# ---------------------------------------------------------------------------

async def enroll_student(user_id: int, course_id: int) -> dict:
    """Idempotent — re-running /aysaenroll on an existing enrollment just
    returns it unchanged rather than resetting progress."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO aysa_enrollments (user_id, course_id)
            VALUES ($1, $2)
            ON CONFLICT (user_id, course_id) DO NOTHING
            """,
            user_id, course_id,
        )
        row = await conn.fetchrow(
            "SELECT * FROM aysa_enrollments WHERE user_id = $1 AND course_id = $2",
            user_id, course_id,
        )
        return dict(row)


async def get_enrollment(user_id: int, course_id: int) -> dict | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM aysa_enrollments WHERE user_id = $1 AND course_id = $2",
            user_id, course_id,
        )
        return dict(row) if row else None


async def get_active_enrollments_for_user(user_id: int) -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.*, c.title AS course_title FROM aysa_enrollments e
            JOIN aysa_courses c ON c.id = e.course_id
            WHERE e.user_id = $1 AND e.status = 'active'
            ORDER BY e.enrolled_at
            """,
            user_id,
        )
        return [dict(r) for r in rows]


async def list_all_active_enrollments() -> list[dict]:
    """Used by the hybrid-nudge background loop in cogs/aysa_courses.py."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM aysa_enrollments WHERE status = 'active'")
        return [dict(r) for r in rows]


async def touch_enrollment_activity(user_id: int, course_id: int) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE aysa_enrollments SET last_activity_at = now() WHERE user_id = $1 AND course_id = $2",
            user_id, course_id,
        )


async def mark_enrollment_nudged(user_id: int, course_id: int) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE aysa_enrollments SET last_nudged_at = now() WHERE user_id = $1 AND course_id = $2",
            user_id, course_id,
        )


async def advance_enrollment(user_id: int, course_id: int, new_lesson_index: int) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE aysa_enrollments
            SET current_lesson_index = $3, last_activity_at = now(), last_nudged_at = NULL
            WHERE user_id = $1 AND course_id = $2
            """,
            user_id, course_id, new_lesson_index,
        )


async def complete_enrollment(user_id: int, course_id: int) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE aysa_enrollments SET status = 'completed' WHERE user_id = $1 AND course_id = $2",
            user_id, course_id,
        )


async def mark_lesson_delivered(user_id: int, course_id: int, lesson_index: int) -> dict:
    """Called the moment a lesson's content is actually sent to a student
    (enrollment, /aysanext, or the deliver_next_lesson chat tool) — creates
    the progress row a step before watched_at/discussed_at get set, so the
    nudge loop and chat tools can tell 'sent but not yet watched' apart
    from 'not sent yet' (no row at all)."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO aysa_lesson_progress (user_id, course_id, lesson_index, delivered_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (user_id, course_id, lesson_index) DO UPDATE
            SET delivered_at = COALESCE(aysa_lesson_progress.delivered_at, now())
            RETURNING *
            """,
            user_id, course_id, lesson_index,
        )
        return dict(row)


async def mark_lesson_watched(user_id: int, course_id: int, lesson_index: int) -> dict:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO aysa_lesson_progress (user_id, course_id, lesson_index, watched_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (user_id, course_id, lesson_index) DO UPDATE
            SET watched_at = now()
            RETURNING *
            """,
            user_id, course_id, lesson_index,
        )
        return dict(row)


async def get_lesson_progress(user_id: int, course_id: int, lesson_index: int) -> dict | None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM aysa_lesson_progress
            WHERE user_id = $1 AND course_id = $2 AND lesson_index = $3
            """,
            user_id, course_id, lesson_index,
        )
        return dict(row) if row else None


async def mark_lesson_discussed(user_id: int, course_id: int, lesson_index: int, comprehension_notes: str) -> None:
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO aysa_lesson_progress (user_id, course_id, lesson_index, discussed_at, comprehension_notes)
            VALUES ($1, $2, $3, now(), $4)
            ON CONFLICT (user_id, course_id, lesson_index) DO UPDATE
            SET discussed_at = now(), comprehension_notes = $4
            """,
            user_id, course_id, lesson_index, comprehension_notes,
        )


# ---------------------------------------------------------------------------
# Aysa — knowledge library (books/papers/PDFs, pgvector search)
# ---------------------------------------------------------------------------
# Every function here is only called when db.KNOWLEDGE_LIBRARY_AVAILABLE is
# True (checked by the caller) — no extra guarding needed within these.

async def add_knowledge_source(title: str, added_by: int) -> dict:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO aysa_knowledge_sources (title, added_by) VALUES ($1, $2) RETURNING *",
            title, added_by,
        )
        return dict(row)


async def add_knowledge_chunk(source_id: int, chunk_index: int, content: str, embedding_literal: str) -> None:
    """embedding_literal is a pgvector text literal, e.g. '[0.01,-0.02,...]'
    — built by utils/aysa_knowledge.py from the raw float list. Passed as a
    plain string parameter and cast with ::vector in SQL since asyncpg has
    no built-in vector codec; no server-side codec registration needed."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO aysa_knowledge_chunks (source_id, chunk_index, content, embedding)
            VALUES ($1, $2, $3, $4::vector)
            """,
            source_id, chunk_index, content, embedding_literal,
        )


async def search_knowledge_chunks(query_embedding_literal: str, top_k: int = 5) -> list[dict]:
    """Cosine-distance nearest-neighbor search (pgvector's <=> operator).
    No ivfflat index — fine at the scale a hand-curated book library
    reaches; add one later if this ever grows into the tens of thousands
    of chunks."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT k.content, k.chunk_index, s.title AS source_title,
                   k.embedding <=> $1::vector AS distance
            FROM aysa_knowledge_chunks k
            JOIN aysa_knowledge_sources s ON s.id = k.source_id
            ORDER BY k.embedding <=> $1::vector
            LIMIT $2
            """,
            query_embedding_literal, top_k,
        )
        return [dict(r) for r in rows]


async def list_knowledge_sources() -> list[dict]:
    pool = _require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.*, count(k.id) AS chunk_count
            FROM aysa_knowledge_sources s
            LEFT JOIN aysa_knowledge_chunks k ON k.source_id = s.id
            GROUP BY s.id ORDER BY s.created_at DESC
            """
        )
        return [dict(r) for r in rows]


async def delete_knowledge_source(source_id: int) -> bool:
    """Cascades to aysa_knowledge_chunks via ON DELETE CASCADE."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM aysa_knowledge_sources WHERE id = $1", source_id)
        return result.endswith(" 1")