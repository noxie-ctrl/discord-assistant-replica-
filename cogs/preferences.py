"""
cogs/preferences.py

Lets members set how Lucy talks to them personally:
  - /mysettings language:<lang> style:<style>  — stored on their user_profiles row
  - /myprofile                                  — shows what Lucy currently
    remembers about them (notes + preferences), for transparency
  - /feedbackstats                              — owner/mod-only rollup of
    reaction-based feedback (👍/👎ing Lucy's own replies)
"""

import discord
from discord import app_commands
from discord.ext import commands

from utils import database as db


RESPONSE_STYLES = [
    app_commands.Choice(name="Concise", value="concise, short answers"),
    app_commands.Choice(name="Detailed", value="detailed, thorough explanations"),
    app_commands.Choice(name="Casual", value="very casual and informal"),
    app_commands.Choice(name="Formal", value="more formal and professional"),
]


class Preferences(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="mysettings", description="Set how Lucy talks to you personally")
    @app_commands.describe(
        language="Reply to you in this language by default (e.g. 'Hindi', 'Spanish', 'English')",
        style="Preferred response style",
    )
    @app_commands.choices(style=RESPONSE_STYLES)
    async def mysettings(
        self,
        interaction: discord.Interaction,
        language: str | None = None,
        style: app_commands.Choice[str] | None = None,
    ):
        if language is None and style is None:
            await interaction.response.send_message(
                "Give me at least one of `language` or `style` to update.", ephemeral=True
            )
            return

        await db.touch_profile(interaction.guild_id, interaction.user.id, str(interaction.user), interaction.user.display_name)
        await db.set_user_preference(
            interaction.guild_id,
            interaction.user.id,
            preferred_language=language,
            response_style=style.value if style else None,
        )
        await interaction.response.send_message("Got it — updated your preferences.", ephemeral=True)

    @app_commands.command(name="myprofile", description="See what Lucy remembers about you")
    async def myprofile(self, interaction: discord.Interaction):
        profile = await db.get_profile(interaction.guild_id, interaction.user.id)
        if profile is None:
            await interaction.response.send_message(
                "I don't have anything on you yet — chat with me a bit first.", ephemeral=True
            )
            return

        embed = discord.Embed(title=f"What I've got on {interaction.user.display_name}", color=discord.Color.blurple())
        embed.add_field(name="Messages seen", value=str(profile.get("message_count", 0)), inline=True)
        embed.add_field(name="Preferred language", value=profile.get("preferred_language") or "not set", inline=True)
        embed.add_field(name="Response style", value=profile.get("response_style") or "not set", inline=True)
        embed.add_field(name="Notes", value=profile.get("notes") or "(nothing noted yet)", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="feedbackstats", description="See Lucy's feedback stats (mod/owner only)")
    async def feedbackstats(self, interaction: discord.Interaction):
        if not (interaction.user.guild_permissions.manage_guild or interaction.user.id == getattr(self.bot, "owner_id", None)):
            await interaction.response.send_message("You need Manage Server permission for this.", ephemeral=True)
            return

        summary = await db.get_feedback_summary(interaction.guild_id)
        negatives = await db.get_recent_negative_feedback(interaction.guild_id, limit=5)

        embed = discord.Embed(title="Lucy feedback summary", color=discord.Color.orange())
        embed.add_field(name="👍", value=str(summary.get("up", 0)), inline=True)
        embed.add_field(name="👎", value=str(summary.get("down", 0)), inline=True)
        if negatives:
            recent = "\n".join(f"- {n['message_snippet'][:80]}" for n in negatives)
            embed.add_field(name="Recent 👎 replies", value=recent, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Preferences(bot))