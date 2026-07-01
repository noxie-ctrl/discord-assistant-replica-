import discord
from discord import app_commands
from discord.ext import commands
from datetime import timedelta

from utils import database as db
from utils.permissions import is_admin_or_mod, is_admin_or_mod_ctx


async def log_action(bot: commands.Bot, guild: discord.Guild, embed: discord.Embed):
    settings = await db.get_guild_settings(guild.id)
    channel_id = settings.get("log_channel_id")
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                pass


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- KICK ----------
    @app_commands.command(name="kick", description="Kick a member from the server")
    @is_admin_or_mod()
    async def kick_slash(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        await member.kick(reason=reason)
        embed = discord.Embed(title="👢 Member Kicked", color=discord.Color.orange())
        embed.add_field(name="User", value=f"{member} ({member.id})")
        embed.add_field(name="Moderator", value=interaction.user.mention)
        embed.add_field(name="Reason", value=reason, inline=False)
        await interaction.response.send_message(embed=embed)
        await log_action(self.bot, interaction.guild, embed)

    @commands.command(name="kick")
    @is_admin_or_mod_ctx()
    async def kick_prefix(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        await member.kick(reason=reason)
        embed = discord.Embed(title="👢 Member Kicked", color=discord.Color.orange())
        embed.add_field(name="User", value=f"{member} ({member.id})")
        embed.add_field(name="Moderator", value=ctx.author.mention)
        embed.add_field(name="Reason", value=reason, inline=False)
        await ctx.send(embed=embed)
        await log_action(self.bot, ctx.guild, embed)

    # ---------- BAN ----------
    @app_commands.command(name="ban", description="Ban a member from the server")
    @is_admin_or_mod()
    async def ban_slash(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        await member.ban(reason=reason)
        embed = discord.Embed(title="🔨 Member Banned", color=discord.Color.red())
        embed.add_field(name="User", value=f"{member} ({member.id})")
        embed.add_field(name="Moderator", value=interaction.user.mention)
        embed.add_field(name="Reason", value=reason, inline=False)
        await interaction.response.send_message(embed=embed)
        await log_action(self.bot, interaction.guild, embed)

    @commands.command(name="ban")
    @is_admin_or_mod_ctx()
    async def ban_prefix(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        await member.ban(reason=reason)
        embed = discord.Embed(title="🔨 Member Banned", color=discord.Color.red())
        embed.add_field(name="User", value=f"{member} ({member.id})")
        embed.add_field(name="Moderator", value=ctx.author.mention)
        embed.add_field(name="Reason", value=reason, inline=False)
        await ctx.send(embed=embed)
        await log_action(self.bot, ctx.guild, embed)

    # ---------- UNBAN ----------
    @app_commands.command(name="unban", description="Unban a user by ID")
    @is_admin_or_mod()
    async def unban_slash(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
        user = discord.Object(id=int(user_id))
        await interaction.guild.unban(user, reason=reason)
        embed = discord.Embed(title="✅ Member Unbanned", color=discord.Color.green())
        embed.add_field(name="User ID", value=user_id)
        embed.add_field(name="Moderator", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed)
        await log_action(self.bot, interaction.guild, embed)

    # ---------- MUTE (timeout) ----------
    @app_commands.command(name="mute", description="Timeout a member (minutes)")
    @is_admin_or_mod()
    async def mute_slash(self, interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "No reason provided"):
        await member.timeout(timedelta(minutes=minutes), reason=reason)
        embed = discord.Embed(title="🔇 Member Muted", color=discord.Color.dark_orange())
        embed.add_field(name="User", value=f"{member} ({member.id})")
        embed.add_field(name="Duration", value=f"{minutes} min")
        embed.add_field(name="Moderator", value=interaction.user.mention)
        embed.add_field(name="Reason", value=reason, inline=False)
        await interaction.response.send_message(embed=embed)
        await log_action(self.bot, interaction.guild, embed)

    @commands.command(name="mute")
    @is_admin_or_mod_ctx()
    async def mute_prefix(self, ctx: commands.Context, member: discord.Member, minutes: int, *, reason: str = "No reason provided"):
        await member.timeout(timedelta(minutes=minutes), reason=reason)
        embed = discord.Embed(title="🔇 Member Muted", color=discord.Color.dark_orange())
        embed.add_field(name="User", value=f"{member} ({member.id})")
        embed.add_field(name="Duration", value=f"{minutes} min")
        embed.add_field(name="Moderator", value=ctx.author.mention)
        embed.add_field(name="Reason", value=reason, inline=False)
        await ctx.send(embed=embed)
        await log_action(self.bot, ctx.guild, embed)

    # ---------- UNMUTE ----------
    @app_commands.command(name="unmute", description="Remove timeout from a member")
    @is_admin_or_mod()
    async def unmute_slash(self, interaction: discord.Interaction, member: discord.Member):
        await member.timeout(None)
        embed = discord.Embed(title="🔊 Member Unmuted", color=discord.Color.green())
        embed.add_field(name="User", value=f"{member} ({member.id})")
        embed.add_field(name="Moderator", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed)
        await log_action(self.bot, interaction.guild, embed)

    # ---------- WARN ----------
    @app_commands.command(name="warn", description="Warn a member")
    @is_admin_or_mod()
    async def warn_slash(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        await db.add_warning(interaction.guild.id, member.id, interaction.user.id, reason)
        embed = discord.Embed(title="⚠️ Member Warned", color=discord.Color.yellow())
        embed.add_field(name="User", value=f"{member} ({member.id})")
        embed.add_field(name="Moderator", value=interaction.user.mention)
        embed.add_field(name="Reason", value=reason, inline=False)
        await interaction.response.send_message(embed=embed)
        await log_action(self.bot, interaction.guild, embed)
        try:
            await member.send(f"You were warned in **{interaction.guild.name}**: {reason}")
        except discord.Forbidden:
            pass

    @app_commands.command(name="warnings", description="List a member's warnings")
    async def warnings_slash(self, interaction: discord.Interaction, member: discord.Member):
        warns = await db.get_warnings(interaction.guild.id, member.id)
        if not warns:
            await interaction.response.send_message(f"{member.mention} has no warnings. Clean record!")
            return
        embed = discord.Embed(title=f"⚠️ Warnings for {member}", color=discord.Color.yellow())
        for w in warns[:10]:
            embed.add_field(name=f"#{w['id']}", value=w["reason"], inline=False)
        await interaction.response.send_message(embed=embed)

    # ---------- PURGE ----------
    @app_commands.command(name="purge", description="Delete a number of recent messages")
    @is_admin_or_mod()
    async def purge_slash(self, interaction: discord.Interaction, amount: int):
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"🧹 Deleted {len(deleted)} messages.", ephemeral=True)

    @commands.command(name="purge")
    @is_admin_or_mod_ctx()
    async def purge_prefix(self, ctx: commands.Context, amount: int):
        deleted = await ctx.channel.purge(limit=amount + 1)
        msg = await ctx.send(f"🧹 Deleted {len(deleted) - 1} messages.")
        await msg.delete(delay=3)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("🚫 You don't have permission to do that.", ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
