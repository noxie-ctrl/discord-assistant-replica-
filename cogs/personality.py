import discord
from discord import app_commands
from discord.ext import commands

from utils import database as db
from utils.permissions import is_admin_or_mod

EDITABLE_FIELDS = ["name", "age", "pronouns", "role", "traits", "backstory", "speaking_style", "boundaries"]


class Personality(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setpersonality", description="Edit one field of Lucy's personality profile")
    @is_admin_or_mod()
    @app_commands.choices(field=[app_commands.Choice(name=f, value=f) for f in EDITABLE_FIELDS])
    async def setpersonality(self, interaction: discord.Interaction, field: app_commands.Choice[str], value: str):
        await db.set_personality_field(interaction.guild.id, field.value, value)
        await interaction.response.send_message(f"✅ Updated **{field.value}** → {value}")

    @app_commands.command(name="profile", description="Show Lucy's current personality profile")
    async def profile(self, interaction: discord.Interaction):
        profile = await db.get_personality(interaction.guild.id)
        embed = discord.Embed(title=f"💁‍♀️ {profile.get('name')}'s Profile", color=discord.Color.pink())
        for field in EDITABLE_FIELDS:
            embed.add_field(name=field.replace("_", " ").title(), value=profile.get(field, "—"), inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="resetpersonality", description="Reset Lucy's personality to the default")
    @is_admin_or_mod()
    async def resetpersonality(self, interaction: discord.Interaction):
        import json
        with open(db.DEFAULT_PERSONALITY_PATH, "r") as f:
            default = json.load(f)
        for field, value in default.items():
            await db.set_personality_field(interaction.guild.id, field, value)
        await interaction.response.send_message("✅ Lucy's personality has been reset to default.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Personality(bot))