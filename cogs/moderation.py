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
        try:
            await member.kick(reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"🚫 I can't kick {member.mention} — their role is at or above mine, or I'm missing "
                "Kick Members permission.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(title="👢 Member Kicked", color=discord.Color.orange())
        embed.add_field(name="User", value=f"{member} ({member.id})")
        embed.add_field(name="Moderator", value=interaction.user.mention)
        embed.add_field(name="Reason", value=reason, inline=False)
        await interaction.response.send_message(embed=embed)
        await log_action(self.bot, interaction.guild, embed)

    @commands.command(name="kick")
    @is_admin_or_mod_ctx()
    async def kick_prefix(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        try:
            await member.kick(reason=reason)
        except discord.Forbidden:
            await ctx.send(
                f"🚫 I can't kick {member.mention} — their role is at or above mine, or I'm missing "
                "Kick Members permission."
            )
            return
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
        try:
            await member.ban(reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"🚫 I can't ban {member.mention} — their role is at or above mine, or I'm missing "
                "Ban Members permission.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(title="🔨 Member Banned", color=discord.Color.red())
        embed.add_field(name="User", value=f"{member} ({member.id})")
        embed.add_field(name="Moderator", value=interaction.user.mention)
        embed.add_field(name="Reason", value=reason, inline=False)
        await interaction.response.send_message(embed=embed)
        await log_action(self.bot, interaction.guild, embed)

    @commands.command(name="ban")
    @is_admin_or_mod_ctx()
    async def ban_prefix(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        try:
            await member.ban(reason=reason)
        except discord.Forbidden:
            await ctx.send(
                f"🚫 I can't ban {member.mention} — their role is at or above mine, or I'm missing "
                "Ban Members permission."
            )
            return
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
        try:
            parsed_id = int(user_id.strip())
        except ValueError:
            await interaction.response.send_message(
                f"⚠️ '{user_id}' isn't a valid user ID — it should be all digits "
                "(right-click a name → Copy User ID, or check the ban list).",
                ephemeral=True,
            )
            return

        user = discord.Object(id=parsed_id)
        try:
            await interaction.guild.unban(user, reason=reason)
        except discord.NotFound:
            await interaction.response.send_message(
                f"⚠️ No ban found for user ID {parsed_id} — they may already be unbanned, or that ID is wrong.",
                ephemeral=True,
            )
            return
        except discord.Forbidden:
            await interaction.response.send_message(
                "🚫 I don't have permission to unban members here.", ephemeral=True
            )
            return

        embed = discord.Embed(title="✅ Member Unbanned", color=discord.Color.green())
        embed.add_field(name="User ID", value=user_id)
        embed.add_field(name="Moderator", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed)
        await log_action(self.bot, interaction.guild, embed)

    # ---------- MUTE (timeout) ----------
    @app_commands.command(name="mute", description="Timeout a member (minutes)")
    @app_commands.describe(minutes="1 to 40320 (28 days) — Discord's own hard cap on timeouts")
    @is_admin_or_mod()
    async def mute_slash(
        self, interaction: discord.Interaction, member: discord.Member,
        minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason provided",
    ):
        try:
            await member.timeout(timedelta(minutes=minutes), reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"🚫 I can't time out {member.mention} — their role is at or above mine, or I'm missing "
                "Timeout Members permission.",
                ephemeral=True,
            )
            return
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
        # Discord's own hard cap on timeouts — the slash version bounds
        # this via app_commands.Range, but the prefix version takes a
        # plain int, so it needs the same check done by hand.
        if not (1 <= minutes <= 40320):
            await ctx.send("⚠️ Minutes must be between 1 and 40320 (28 days — Discord's own timeout cap).")
            return
        try:
            await member.timeout(timedelta(minutes=minutes), reason=reason)
        except discord.Forbidden:
            await ctx.send(
                f"🚫 I can't time out {member.mention} — their role is at or above mine, or I'm missing "
                "Timeout Members permission."
            )
            return
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
        try:
            await member.timeout(None)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"🚫 I can't remove {member.mention}'s timeout — their role is at or above mine, or I'm "
                "missing Timeout Members permission.",
                ephemeral=True,
            )
            return
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
    @app_commands.describe(amount="1 to 100 — Discord's own bulk-delete cap per request")
    @is_admin_or_mod()
    async def purge_slash(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
        await interaction.response.defer(ephemeral=True)
        try:
            deleted = await interaction.channel.purge(limit=amount)
        except discord.Forbidden:
            await interaction.followup.send("🚫 I don't have permission to delete messages here.", ephemeral=True)
            return
        except discord.HTTPException as e:
            # Most common real cause: some of those messages are older than
            # 14 days — Discord's bulk-delete endpoint refuses those outright
            # rather than silently skipping them.
            await interaction.followup.send(
                f"⚠️ Couldn't finish purging (messages older than 14 days can't be bulk-deleted): {e}",
                ephemeral=True,
            )
            return
        await interaction.followup.send(f"🧹 Deleted {len(deleted)} messages.", ephemeral=True)

    @commands.command(name="purge")
    @is_admin_or_mod_ctx()
    async def purge_prefix(self, ctx: commands.Context, amount: int):
        if not (1 <= amount <= 100):
            await ctx.send("⚠️ Amount must be between 1 and 100 (Discord's own bulk-delete cap per request).")
            return
        try:
            deleted = await ctx.channel.purge(limit=amount + 1)
        except discord.Forbidden:
            await ctx.send("🚫 I don't have permission to delete messages here.")
            return
        except discord.HTTPException as e:
            await ctx.send(f"⚠️ Couldn't finish purging (messages older than 14 days can't be bulk-deleted): {e}")
            return
        msg = await ctx.send(f"🧹 Deleted {len(deleted) - 1} messages.")
        await msg.delete(delay=3)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("🚫 You don't have permission to do that.", ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))