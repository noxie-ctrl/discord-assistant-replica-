import aiosqlite
import json
import os
import time

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "lucy.db")

DEFAULT_PERSONALITY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "personality_default.json"
)


async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                prefix TEXT DEFAULT '!',
                log_channel_id INTEGER,
                welcome_channel_id INTEGER,
                welcome_message TEXT,
                chat_trigger_mode TEXT DEFAULT 'mention',
                chat_channel_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS personality (
                guild_id INTEGER PRIMARY KEY,
                profile_json TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                user_id INTEGER,
                moderator_id INTEGER,
                reason TEXT,
                created_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                channel_id INTEGER,
                role TEXT,
                content TEXT,
                created_at INTEGER
            )
        """)
        await db.commit()


async def get_guild_settings(guild_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
        row = await cur.fetchone()
        if row is None:
            await db.execute("INSERT INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
            await db.commit()
            return {
                "guild_id": guild_id,
                "prefix": "!",
                "log_channel_id": None,
                "welcome_channel_id": None,
                "welcome_message": None,
                "chat_trigger_mode": "mention",
                "chat_channel_id": None,
            }
        return dict(row)


async def update_guild_setting(guild_id: int, **kwargs):
    await get_guild_settings(guild_id)  # ensure row exists
    keys = ", ".join(f"{k} = ?" for k in kwargs.keys())
    values = list(kwargs.values()) + [guild_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE guild_settings SET {keys} WHERE guild_id = ?", values)
        await db.commit()


async def get_personality(guild_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT profile_json FROM personality WHERE guild_id = ?", (guild_id,))
        row = await cur.fetchone()
        if row is None:
            with open(DEFAULT_PERSONALITY_PATH, "r") as f:
                default = json.load(f)
            await db.execute(
                "INSERT INTO personality (guild_id, profile_json) VALUES (?, ?)",
                (guild_id, json.dumps(default)),
            )
            await db.commit()
            return default
        return json.loads(row["profile_json"])


async def set_personality_field(guild_id: int, field: str, value: str):
    profile = await get_personality(guild_id)
    profile[field] = value
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO personality (guild_id, profile_json) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET profile_json = excluded.profile_json",
            (guild_id, json.dumps(profile)),
        )
        await db.commit()


async def add_warning(guild_id: int, user_id: int, moderator_id: int, reason: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, moderator_id, reason, int(time.time())),
        )
        await db.commit()


async def get_warnings(guild_id: int, user_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC",
            (guild_id, user_id),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def add_chat_memory(guild_id: int, channel_id: int, role: str, content: str, keep_last: int = 12):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO chat_memory (guild_id, channel_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, channel_id, role, content, int(time.time())),
        )
        await db.commit()
        # trim old entries beyond keep_last per channel
        cur = await db.execute(
            "SELECT id FROM chat_memory WHERE guild_id = ? AND channel_id = ? ORDER BY created_at DESC",
            (guild_id, channel_id),
        )
        rows = await cur.fetchall()
        if len(rows) > keep_last:
            old_ids = [r[0] for r in rows[keep_last:]]
            await db.executemany("DELETE FROM chat_memory WHERE id = ?", [(i,) for i in old_ids])
            await db.commit()


async def get_chat_memory(guild_id: int, channel_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content FROM chat_memory WHERE guild_id = ? AND channel_id = ? ORDER BY created_at ASC",
            (guild_id, channel_id),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
