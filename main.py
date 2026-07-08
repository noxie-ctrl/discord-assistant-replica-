"""
main.py

Entry point for Lucy. Changes in this version:
  - Initializes the asyncpg Postgres pool (Railway) instead of aiosqlite,
    and closes it cleanly on shutdown.
  - Loads two new cogs: games (mini-games) and news (real headlines).
  - Everything else (owner_id wiring, slash sync, prefix commands) is
    unchanged from before.
"""

import os
import asyncio
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

from utils import database as db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lucy.main")

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
# NOT yet added: INTENTS.presences = True — this needs Nox to first enable
# "Presence Intent" in the Discord Developer Portal (Bot tab, alongside the
# two intents above). Required for Max Awareness's status/activity lookup.
# See MAX_AWARENESS_HANDOFF.md, Phase 0. Once that's confirmed done, flip
# this on and INFO_TOOLS' lookup_member (utils/nim_client.py) can start
# returning real status/activity instead of omitting it.

COGS = [
    "cogs.moderation",
    "cogs.utility",
    "cogs.personality",
    "cogs.ai_chat",
    "cogs.games",
    "cogs.news",
    "cogs.preferences",
    "cogs.serverlog",
]


class Lucy(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)

    async def setup_hook(self):
        await db.init_pool()
        logger.info("Postgres pool ready.")

        for cog in COGS:
            try:
                await self.load_extension(cog)
                logger.info("Loaded %s", cog)
            except Exception:
                logger.exception("Failed to load %s", cog)

        owner_id = os.getenv("OWNER_ID", "").strip()
        if owner_id:
            self.owner_id = int(owner_id)
            logger.info("Owner ID set to %s", self.owner_id)
        else:
            logger.warning("OWNER_ID not set — owner-specific persona won't activate.")

        synced = await self.tree.sync()
        logger.info("Synced %d slash commands.", len(synced))

    async def on_ready(self):
        # Lucy sometimes "plays" guessnumber as an AI opponent — record_game_result
        # gets called with her own user id in that case, and this tells the
        # database layer to skip the (meaningless) relationship-score bump for it.
        db.set_bot_user_id(self.user.id)
        logger.info("Logged in as %s (id %s)", self.user, self.user.id)

    async def close(self):
        await db.close_pool()
        await super().close()


async def main():
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set.")

    bot = Lucy()
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())