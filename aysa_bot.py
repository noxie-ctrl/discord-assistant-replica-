"""
aysa_bot.py

Entry point for Aysa, the psychology mentor/education bot — a separate
Discord application from both Lucy and the GitHub bot, running in the
SAME process (see main.py's main(), which starts all bots concurrently
via asyncio.gather). Shares Lucy's Postgres pool, aiohttp session, rate
limiter, and AI client wrappers — no second copy of any of that. Zero
extra hosting cost: same service, same process, just a third gateway
connection with its own token.

Scope: role-gated 1:1 mentoring conversation with persistent memory
(cogs/aysa_chat.py) and a structured course/curriculum system
(cogs/aysa_courses.py). Does NOT carry Lucy's persona, moderation, games,
or the GitHub bot's repo tools — deliberately its own thing.

Access is gated to one specific role in one specific server — see
AYSA_GUILD_ID / AYSA_ROLE_ID in utils/permissions.py's is_aysa_authorized.
Both DMs and @mentions in that server are honored once someone has the
role; slash commands (enrollment, course browsing, etc.) are guild-only,
same convention as every other command in this project.

Requires AYSA_BOT_TOKEN. If it's not set, build() returns None and
main.py just runs without Aysa — this bot is additive, not a hard
dependency, same pattern as GITHUB_BOT_TOKEN.
"""

import os
import logging

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("lucy.aysa_bot")

INTENTS = discord.Intents.default()
INTENTS.message_content = True  # needed to read DM/mention-triggered chat messages
INTENTS.members = True          # needed for guild.fetch_member / role checks in permissions.is_aysa_authorized

COGS = [
    "cogs.aysa_chat",     # persona, DM/mention chat loop, memory, crisis safety, knowledge library
    "cogs.aysa_courses",  # course/lesson admin, enrollment, progress, hybrid nudge loop
]


class AysaBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!aysa", intents=INTENTS)
        self.tree.on_error = self.on_app_command_error

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CheckFailure):
            message = "🚫 This is Aysa's space — you'll need the right role (and to be in her server) to use this."
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
        # before any bot starts (see main.py's main()). init_pool() is
        # idempotent, so calling it again here would be harmless, but
        # skipping it makes the "one shared pool" intent explicit.
        for cog in COGS:
            try:
                await self.load_extension(cog)
                logger.info("AysaBot loaded %s", cog)
            except Exception:
                logger.exception("AysaBot failed to load %s", cog)

        synced = await self.tree.sync()
        logger.info("AysaBot synced %d slash commands.", len(synced))

    async def on_ready(self):
        logger.info("AysaBot logged in as %s (id %s)", self.user, self.user.id)


def build() -> "AysaBot | None":
    token = os.getenv("AYSA_BOT_TOKEN", "").strip()
    if not token:
        logger.warning("AYSA_BOT_TOKEN not set — Aysa will not start.")
        return None
    if not os.getenv("AYSA_GUILD_ID", "").strip() or not os.getenv("AYSA_ROLE_ID", "").strip():
        logger.warning(
            "AYSA_GUILD_ID / AYSA_ROLE_ID not set — Aysa will start but deny everyone "
            "(is_aysa_authorized fails closed without a configured role gate)."
        )
    return AysaBot()
