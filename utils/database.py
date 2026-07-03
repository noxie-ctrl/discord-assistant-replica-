"""
utils/database.py

Postgres-backed persistence layer for Lucy, using Railway's Postgres plugin.

v2 fix: the personality/guild_settings schema now matches the field names
your actual cogs/personality.py and cogs/utility.py use (pronouns, role,
speaking_style, boundaries / chat_trigger_mode) — the first version guessed
wrong names and would have broken /setpersonality and /setchattrigger.
"""

import os
import logging

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
    welcome_message TEXT
);

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

CREATE INDEX IF NOT EXISTS idx_chat_memory_guild_channel
    ON chat_memory (guild_id, channel_id, created_at DESC);
"""

# Tables above only get created if they don't exist — they already exist in
# production from the previous deploy, so new columns need explicit ALTERs.
MIGRATIONS = """
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS username TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS preferred_language TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS response_style TEXT;
"""


async def init_pool():
    """Call once on bot startup."""
    global _pool
    dsn = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. In Railway, add a variable reference "
            "to your Postgres service (e.g. ${{Postgres.DATABASE_URL}}) on "
            "the worker service."
        )
    if dsn.startswith("postgres://"):
        dsn = dsn.replace("postgres://", "postgresql://", 1)

    _pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)
        await conn.execute(MIGRATIONS)
    logger.info("Database pool initialized, schema ensured, migrations applied.")
    return _pool


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


async def update_guild_setting(guild_id: int, **kwargs):
    """update_guild_setting(guild_id, chat_trigger_mode='mention', chat_channel_id=123)"""
    if not kwargs:
        return
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
# Personality
# ---------------------------------------------------------------------------

async def get_personality(guild_id: int) -> dict:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM personality WHERE guild_id = $1", guild_id
        )
        if row is None:
            await conn.execute(
                "INSERT INTO personality (guild_id) VALUES ($1) "
                "ON CONFLICT (guild_id) DO NOTHING",
                guild_id,
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
    """Upsert a profile, bump message_count + last_seen. Stores the immutable
    Discord username alongside the (changeable) display name and the id, so
    long-term memory is keyed on something more durable than a nickname.
    Returns the fresh row."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO user_profiles (guild_id, user_id, username, display_name, message_count, last_seen)
            VALUES ($1, $2, $3, $4, 1, now())
            ON CONFLICT (guild_id, user_id) DO UPDATE
            SET username = $3,
                display_name = $4,
                message_count = user_profiles.message_count + 1,
                last_seen = now()
            RETURNING *
            """,
            guild_id, user_id, username, display_name,
        )
        return dict(row)


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


async def adjust_relationship_score(guild_id: int, user_id: int, delta: int):
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_profiles SET relationship_score = relationship_score + $3 "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id, delta,
        )


# ---------------------------------------------------------------------------
# Mini-game stats
# ---------------------------------------------------------------------------

async def record_game_result(guild_id: int, user_id: int, game: str, result: str):
    """result is 'win' | 'loss' | 'draw'"""
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


async def get_game_stats(guild_id: int, user_id: int, game: str) -> dict:
    pool = _require_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM game_stats WHERE guild_id = $1 AND user_id = $2 AND game = $3",
            guild_id, user_id, game,
        )
        return dict(row) if row else {"wins": 0, "losses": 0, "draws": 0}


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


# ---------------------------------------------------------------------------
# Feedback (reaction-based, feeds the model rather than "training" it)
# ---------------------------------------------------------------------------

async def add_feedback(guild_id: int, user_id: int, channel_id: int,
                         message_snippet: str, rating: str, note: str | None = None):
    """rating is 'up' or 'down'."""
    pool = _require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO feedback (guild_id, user_id, channel_id, message_snippet, rating, note) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            guild_id, user_id, channel_id, message_snippet, rating, note,
        )


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