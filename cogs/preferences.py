"""
cogs/preferences.py

Lets members set how Lucy talks to them personally:
  - /mysettings language:<lang> style:<style>  — stored on their user_profiles row
  - /vibecheck                                  — 4-tap forced-choice calibration
    that fast-tracks utils/persona_engine.py's per-user style axes (Lucy adapts
    passively either way — this just speeds it up and lets someone state a
    preference explicitly instead of waiting for it to be inferred)
  - /myprofile                                  — shows what Lucy currently
    remembers about them (notes + preferences + how she's adapted), for
    transparency — the personalization should never feel like it's happening
    behind their back
  - /feedbackstats                              — owner/mod-only rollup of
    reaction-based feedback (👍/👎ing Lucy's own replies)
"""

import discord
from discord import app_commands
from discord.ext import commands

from utils import database as db
from utils import persona_engine


RESPONSE_STYLES = [
    app_commands.Choice(name="Concise", value="concise, short answers"),
    app_commands.Choice(name="Detailed", value="detailed, thorough explanations"),
    app_commands.Choice(name="Casual", value="very casual and informal"),
    app_commands.Choice(name="Formal", value="more formal and professional"),
]


class VibeCheckButton(discord.ui.Button):
    def __init__(self, label: str, delta: dict):
        super().__init__(style=discord.ButtonStyle.secondary, label=label[:80])
        self.delta = delta

    async def callback(self, interaction: discord.Interaction):
        view: "VibeCheckView" = self.view
        if interaction.user.id != view.user_id:
            await interaction.response.send_message(
                "This vibe check isn't yours — run `/vibecheck` for your own.", ephemeral=True
            )
            return
        await view.advance(interaction, self.delta)


class VibeCheckView(discord.ui.View):
    """4 forced-choice questions, one screen at a time, edited in place so
    it reads as a quick tap-through rather than a form. Each answer saves
    immediately (see persona_engine.apply_explicit_deltas), so quitting
    halfway still keeps whatever was answered — nothing is lost or wasted
    by not finishing."""

    def __init__(self, guild_id: int, user_id: int, *, timeout: float = 90.0):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.user_id = user_id
        self.step = 0
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        question = persona_engine.VIBECHECK_QUESTIONS[self.step]
        for option in question["options"]:
            self.add_item(VibeCheckButton(option["label"], option["delta"]))

    def current_embed(self) -> discord.Embed:
        question = persona_engine.VIBECHECK_QUESTIONS[self.step]
        embed = discord.Embed(description=f"**{question['prompt']}**", color=discord.Color.pink())
        embed.set_footer(text=f"Question {self.step + 1}/{len(persona_engine.VIBECHECK_QUESTIONS)} — tap one")
        return embed

    async def advance(self, interaction: discord.Interaction, delta: dict):
        profile_row = await db.get_profile(self.guild_id, self.user_id) or {}
        style_profile, style_confidence = persona_engine.load_profile_row(profile_row)
        style_profile, style_confidence = persona_engine.apply_explicit_deltas(
            style_profile, style_confidence, delta
        )
        await db.save_style_profile(self.guild_id, self.user_id, style_profile, style_confidence)

        self.step += 1
        if self.step >= len(persona_engine.VIBECHECK_QUESTIONS):
            await db.mark_onboarded(self.guild_id, self.user_id)
            self.clear_items()
            done_embed = discord.Embed(
                description=(
                    "That's the vibe check done — I'll keep tuning as we talk. Run "
                    "`/myprofile` anytime to see how I've read you, or `/vibecheck` again to redo it."
                ),
                color=discord.Color.pink(),
            )
            await interaction.response.edit_message(embed=done_embed, view=None)
            self.stop()
            return

        self._build_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


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

    @app_commands.command(name="vibecheck", description="Quick 4-tap calibration so Lucy matches your vibe faster")
    async def vibecheck(self, interaction: discord.Interaction):
        await db.touch_profile(
            interaction.guild_id, interaction.user.id, str(interaction.user), interaction.user.display_name
        )
        view = VibeCheckView(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(embed=view.current_embed(), view=view, ephemeral=True)

    @app_commands.command(name="myprofile", description="See what Lucy remembers about you")
    async def myprofile(self, interaction: discord.Interaction):
        profile = await db.get_profile(interaction.guild_id, interaction.user.id)
        if profile is None:
            await interaction.response.send_message(
                "I don't have anything on you yet — chat with me a bit first.", ephemeral=True
            )
            return

        style_profile, style_confidence = persona_engine.load_profile_row(profile)
        vibe_summary = persona_engine.describe_style_for_user(style_profile, style_confidence)

        embed = discord.Embed(title=f"What I've got on {interaction.user.display_name}", color=discord.Color.blurple())
        embed.add_field(name="Messages seen", value=str(profile.get("message_count", 0)), inline=True)
        embed.add_field(name="Preferred language", value=profile.get("preferred_language") or "not set", inline=True)
        embed.add_field(name="Response style", value=profile.get("response_style") or "not set", inline=True)
        embed.add_field(name="How I've adapted to you", value=vibe_summary, inline=False)
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