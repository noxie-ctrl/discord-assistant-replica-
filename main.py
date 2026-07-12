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
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from utils import database as db
from utils import http

load_dotenv()

# Perf fix (this session): uvloop is a drop-in libuv-backed replacement for
# asyncio's default event loop — commonly a meaningful speedup for an I/O-
# heavy app like this one (constant Discord gateway traffic, HTTP calls to
# NIM/Groq/OpenRouter/GitHub/Postgres) for zero code changes anywhere else.
# Linux/macOS only, which is fine — Railway (this bot's actual deployment
# target) is Linux. Falls back to the standard event loop silently if it's
# not installed (e.g. a Windows dev machine), so this can't break anything.
try:
    import uvloop
    uvloop.install()
except ImportError:
    uvloop = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lucy.main")
if uvloop is not None:
    logger.info("uvloop installed — running on the faster event loop.")

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
# INTENTS.presences is intentionally still off here even though the
# Discord Developer Portal's "Presence Intent" toggle has been enabled
# since the start of this project (confirmed by Nox — the earlier comment
# here claiming it wasn't enabled yet was stale/wrong). The Portal toggle
# only grants the *ability* to request presence data; requesting it via
# INTENTS.presences = True is a separate, deliberate step that isn't taken
# here because nothing in the bot consumes presence data yet (Max
# Awareness Phase 1+ — see MAX_AWARENESS_HANDOFF.md — is what would use
# it). Flipping this on before anything reads presence data would just add
# gateway traffic and member-cache overhead for no benefit, so it stays
# off until that feature actually lands.

COGS = [
    "cogs.moderation",
    "cogs.utility",
    "cogs.personality",
    "cogs.ai_chat",
    "cogs.games",
    "cogs.news",
    "cogs.preferences",
    "cogs.serverlog",
    "cogs.github",
]


class Lucy(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=INTENTS)
        # Global error-handling fix (this session): every cog except
        # Moderation had zero error handling on its slash commands, and no
        # cog had any at all for prefix commands. That meant an unhandled
        # exception anywhere in the bot — games, utility, github, a typo'd
        # role name, a member below the bot in the hierarchy, anything —
        # left the person looking at Discord's bare "This interaction
        # failed" (slash commands) or literal silence (prefix commands),
        # with the actual cause visible only in Railway logs. A cog-level
        # handler (see Moderation.cog_app_command_error) still gets first
        # refusal and can give more specific messaging for its own
        # commands; this is just the bot-wide safety net underneath that.
        self.tree.on_error = self.on_app_command_error

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CheckFailure):
            message = "🚫 You don't have permission to do that."
        else:
            logger.error(
                "Unhandled app command error in /%s",
                getattr(interaction.command, "name", "?"), exc_info=error,
            )
            message = "⚠️ Something went wrong running that — I've logged it, try again in a bit."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass  # interaction's already dead (timed out, etc.) — nothing more to do

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandNotFound):
            return  # not every "!something" someone types is meant as a command
        if isinstance(error, commands.CheckFailure):
            await ctx.send("🚫 You don't have permission to do that.")
            return
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.send(f"⚠️ {error}")
            return
        logger.error("Unhandled prefix command error in !%s", ctx.command, exc_info=error)
        await ctx.send("⚠️ Something went wrong running that — I've logged it.")

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
        await http.close_session()
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