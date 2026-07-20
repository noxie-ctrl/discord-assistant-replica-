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
from github_bot import build as build_github_bot
from aysa_bot import build as build_aysa_bot

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
# Max Awareness, Phase 2: flipped on. lookup_member (INFO_TOOLS in
# nim_client.py) now reads member.status / member.activities — see
# format_member_lookup in cogs/ai_chat.py. Portal toggle was already on;
# this is the code-side half. NERV-HQ is a small single guild, so the
# added gateway/member-cache overhead this comment used to warn about is
# negligible here — revisit if that guild ever grows large enough for
# Discord's large-guild presence chunking behavior to matter.
INTENTS.presences = True
COGS = [
    "cogs.moderation",
    "cogs.utility",
    "cogs.personality",
    "cogs.ai_chat",
    "cogs.games",
    "cogs.news",
    "cogs.preferences",
    "cogs.serverlog",
    # cogs.github moved off Lucy this session — now runs under its own
    # bot identity (github_bot.py). See GITHUB_BOT_TOKEN.
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

        # Rate-limit all slash commands globally: 1 command / 5s / user.
        # Owner bypasses. Cog-level checks (is_admin_or_mod, etc.) still
        # run after this, so permission gates aren't affected.
        from utils.rate_limiter import is_slash_rate_limited

        async def _interaction_check(interaction: discord.Interaction) -> bool:
            if is_slash_rate_limited(interaction.user.id):
                await interaction.response.send_message(
                    "You're using commands too fast — slow down.", ephemeral=True
                )
                return False
            return True

        self.tree.interaction_check = _interaction_check

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


# ---------------------------------------------------------------------------
# Health-check HTTP endpoint (aiohttp.web, in-process)
# ---------------------------------------------------------------------------
# Render/Koyeb/Railway all inject PORT. A free pinger service (UptimeRobot)
# hits this every few minutes to keep the dyno awake. Bound to the same
# event loop as the bot — starts alongside the gateway connection, doesn't
# block it, and shuts down cleanly when the bot closes.
# ---------------------------------------------------------------------------

from aiohttp import web


async def _health_handler(request: web.Request) -> web.Response:
    """Lightweight health check: gateway(s) connected + DB reachable.
    Widened (this session) so a silent GitHub-bot or Aysa disconnect also
    trips Render's probe, not just Lucy's."""
    bot: Lucy = request.app["bot"]
    gh_bot = request.app.get("github_bot")
    aysa_bot = request.app.get("aysa_bot")
    status = {"lucy_gateway": "connected" if bot.is_ready() else "connecting"}
    if gh_bot is not None:
        status["github_bot_gateway"] = "connected" if gh_bot.is_ready() else "connecting"
    if aysa_bot is not None:
        status["aysa_bot_gateway"] = "connected" if aysa_bot.is_ready() else "connecting"

    try:
        pool = db._require_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        status["database"] = "ok"
    except Exception as e:
        status["database"] = f"error: {e}"

    http_status = 200 if status["database"] == "ok" else 503
    return web.json_response(status, status=http_status)


async def _start_health_server(bot: Lucy, github_bot=None, aysa_bot=None) -> web.AppRunner:
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()
    app["bot"] = bot
    app["github_bot"] = github_bot
    app["aysa_bot"] = aysa_bot
    app.router.add_get("/health", _health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("Health endpoint listening on 0.0.0.0:%s/health", port)
    return runner


async def main():
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set.")

    await db.init_pool()  # once, here — before either bot's setup_hook can race to call it
    logger.info("Postgres pool ready (shared by both bots).")

    bot = Lucy()
    github_bot = build_github_bot()  # None if GITHUB_BOT_TOKEN isn't set
    aysa_bot = build_aysa_bot()      # None if AYSA_BOT_TOKEN isn't set

    runner = None
    try:
        runner = await _start_health_server(bot, github_bot, aysa_bot)

        tasks = [_run_lucy(bot, token)]
        if github_bot is not None:
            tasks.append(_run_github_bot(github_bot, os.getenv("GITHUB_BOT_TOKEN", "").strip()))
            logger.info("GitHub bot enabled — starting alongside Lucy.")
        else:
            logger.info("GITHUB_BOT_TOKEN not set — running Lucy only.")
        if aysa_bot is not None:
            tasks.append(_run_aysa_bot(aysa_bot, os.getenv("AYSA_BOT_TOKEN", "").strip()))
            logger.info("Aysa enabled — starting alongside Lucy.")
        else:
            logger.info("AYSA_BOT_TOKEN not set — running without Aysa.")

        await asyncio.gather(*tasks)
    finally:
        if runner is not None:
            await runner.cleanup()
            logger.info("Health endpoint shut down.")


async def _run_lucy(bot: Lucy, token: str):
    async with bot:
        await bot.start(token)


async def _run_github_bot(github_bot, token: str):
    async with github_bot:
        await github_bot.start(token)


async def _run_aysa_bot(aysa_bot, token: str):
    async with aysa_bot:
        await aysa_bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())