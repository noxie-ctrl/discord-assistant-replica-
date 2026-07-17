"""
cogs/github_chat.py

Mention-triggered conversational access to GitHub repo questions and
project context, for the isolated GitHub bot (github_bot.py). Deliberately
minimal compared to Lucy's ai_chat.py — no persona, no chat_memory, no
vent watching, no idle chatter. Get @-mentioned, run a small tool-calling
loop against utils/github_tools.py (shared with Lucy — same schemas AND
same execution), reply.

Also owns /projectlink, /projectinfo, /projectunlink — explicit-set
channel-to-repo mapping (ghbot_projects table), not passive inference: no
background classification, no silent scraping of conversation into a
permanent record.
"""

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils import database as db
from utils import github_tools
from utils import nim_client
from utils.permissions import is_admin_or_mod
from utils.rate_limiter import is_chat_rate_limited

logger = logging.getLogger("lucy.github_chat")

MAX_TOOL_ITERATIONS = 4  # smaller than Lucy's 5 — this bot's questions are narrower in scope
MAX_TOKENS = 500

SYSTEM_PROMPT_TEMPLATE = (
    "{role}\n\n"
    "You have tools to look up linked GitHub repos (README, file tree, code search, "
    "individual files), recent commit/PR activity, and this channel's linked project "
    "info. Use them rather than guessing — if a tool returns an error (e.g. no repo "
    "linked), say so plainly instead of making something up. Keep answers concise and "
    "technical; this is a working channel, not a persona chat."
)


class GitHubChat(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -----------------------------------------------------------------
    # Project linking
    # -----------------------------------------------------------------

    @app_commands.command(name="projectlink", description="Link this channel to a project/repo for context")
    @app_commands.describe(
        repo="owner/repo this channel is about",
        description="Optional short description of what this project/team is working on",
    )
    @is_admin_or_mod()
    async def projectlink(self, interaction: discord.Interaction, repo: str, description: str = ""):
        repo_key = repo.strip().lower()
        await db.set_project_link(
            interaction.guild_id, interaction.channel_id, repo_key, description, interaction.user.id
        )
        await interaction.response.send_message(f"📌 This channel is now linked to `{repo_key}`.")

    @app_commands.command(name="projectinfo", description="Show this channel's linked project info")
    async def projectinfo(self, interaction: discord.Interaction):
        project = await db.get_project_link(interaction.guild_id, interaction.channel_id)
        if not project:
            await interaction.response.send_message("No project linked to this channel yet — use `/projectlink`.")
            return
        embed = discord.Embed(
            title=f"📌 {project['repo']}",
            description=project.get("description") or "\u200b",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="projectunlink", description="Remove this channel's project link")
    @is_admin_or_mod()
    async def projectunlink(self, interaction: discord.Interaction):
        removed = await db.remove_project_link(interaction.guild_id, interaction.channel_id)
        if removed:
            await interaction.response.send_message("🗑️ Project link removed.")
        else:
            await interaction.response.send_message("No project was linked to this channel.", ephemeral=True)

    # -----------------------------------------------------------------
    # Conversational tool loop
    # -----------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if self.bot.user not in message.mentions:
            return
        if is_chat_rate_limited(message.author.id):
            return  # shares Lucy's rate limiter — same protection on total AI-backend load

        async with message.channel.typing():
            await self._handle(message)

    async def _handle(self, message: discord.Message):
        scope = await db.get_ghbot_scope(message.guild.id)
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(role=scope)
        user_text = message.content.replace(f"<@{self.bot.user.id}>", "").replace(
            f"<@!{self.bot.user.id}>", ""
        ).strip()
        if not user_text:
            user_text = "(no text — just a mention)"

        chat_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]

        reply = None
        try:
            for _ in range(MAX_TOOL_ITERATIONS):
                assistant_message = await nim_client.call_nim_with_tools(
                    chat_messages, tools=github_tools.GITHUB_TOOL_SCHEMAS, max_tokens=MAX_TOKENS
                )
                if not assistant_message.get("tool_calls"):
                    reply = (assistant_message.get("content") or "").strip()
                    break

                chat_messages.append(assistant_message)
                for tool_call in assistant_message["tool_calls"]:
                    name = tool_call["function"]["name"]
                    try:
                        args = json.loads(tool_call["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = await github_tools.dispatch(
                        name, args, message.guild, channel_id=message.channel.id
                    )
                    logger.info("GitHub bot tool call: %s -> %s", name, result[:200])
                    chat_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.get("id", ""),
                        "content": result,
                    })

            if reply is None:
                final_message = await nim_client.call_nim_with_tools(
                    chat_messages, tools=None, max_tokens=MAX_TOKENS
                )
                reply = (final_message.get("content") or "").strip()

            if not reply:
                reply = "I looked into that but didn't come up with a clear answer — try rephrasing?"
        except Exception:
            logger.exception(
                "GitHub bot chat handling failed in guild %s channel %s",
                message.guild.id, message.channel.id,
            )
            await message.reply(
                "Sorry, I ran into a problem answering that — try again in a bit?",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=False),
            )
            return

        for chunk_start in range(0, len(reply), 2000):
            await message.channel.send(
                reply[chunk_start:chunk_start + 2000],
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=False),
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(GitHubChat(bot))