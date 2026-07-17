"""
github_bot.py

Entry point for the isolated GitHub bot — a separate Discord application
from Lucy, running in the SAME process (see main.py's main(), which starts
both bots concurrently via asyncio.gather). Shares Lucy's Postgres pool,
aiohttp session, rate limiter, and AI client wrappers (all module-level
singletons in utils/) — no second copy of any of that. Zero extra hosting
cost: same Render service, same process, just a second gateway connection
with its own token.

Scope: repo linking + update posting + weekly digest (cogs/github.py,
moved here from Lucy this session) plus a lightweight conversational path
(cogs/github_chat.py) for mention-triggered questions about linked repos
and project context. Does NOT carry Lucy's persona, chat_memory, vent
watching, idle chatter, or moderation — deliberately narrow.

Requires GITHUB_BOT_TOKEN. If it's not set, build() returns None and
main.py just runs Lucy alone — this bot is additive, not a hard dependency.
"""

import os
import logging

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("lucy.github_bot")

INTENTS = discord.Intents.default()
INTENTS.message_content = True  # needed to see mention-triggered chat messages

COGS = [
    "cogs.github",       # /githublink, /githubunlink, /githublinks, /githubdigest[now], polling, weekly digest
    "cogs.github_chat",  # mention-triggered conversational tool loop + /projectlink, /projectinfo, /projectunlink
]


class GitHubBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!gh", intents=INTENTS)
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
            pass

    async def setup_hook(self):
        # Does NOT call db.init_pool() — main.py already does that once,
        # before either bot starts (see main.py's main()). init_pool() is
        # idempotent now, so it would be harmless to call again here, but
        # skipping it makes the "one shared pool" intent explicit.
        for cog in COGS:
            try:
                await self.load_extension(cog)
                logger.info("GitHubBot loaded %s", cog)
            except Exception:
                logger.exception("GitHubBot failed to load %s", cog)

        synced = await self.tree.sync()
        logger.info("GitHubBot synced %d slash commands.", len(synced))

    async def on_ready(self):
        logger.info("GitHubBot logged in as %s (id %s)", self.user, self.user.id)


def build() -> "GitHubBot | None":
    token = os.getenv("GITHUB_BOT_TOKEN", "").strip()
    if not token:
        logger.warning("GITHUB_BOT_TOKEN not set — GitHub bot will not start.")
        return None
    return GitHubBot()