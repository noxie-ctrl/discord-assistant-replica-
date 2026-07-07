import discord
from discord import app_commands
from discord.ext import commands

from utils import database as db
from utils.permissions import is_admin_or_mod


class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- WELCOME ----------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        settings = await db.get_guild_settings(member.guild.id)
        channel_id = settings.get("welcome_channel_id")
        if not channel_id:
            return
        channel = member.guild.get_channel(channel_id)
        if not channel:
            return
        msg = settings.get("welcome_message") or "Welcome {member} to **{server}**! 🎉"
        text = msg.replace("{member}", member.mention).replace("{server}", member.guild.name)
        try:
            await channel.send(text)
        except discord.Forbidden:
            pass

    @app_commands.command(name="setwelcome", description="Set the welcome channel and message")
    @is_admin_or_mod()
    async def setwelcome(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str = "Welcome {member} to **{server}**! 🎉"):
        await db.update_guild_setting(interaction.guild.id, welcome_channel_id=channel.id, welcome_message=message)
        await interaction.response.send_message(f"✅ Welcome messages will go to {channel.mention}.\nUse `{{member}}` and `{{server}}` as placeholders.")

    # ---------- LOG CHANNEL ----------
    @app_commands.command(name="setlogchannel", description="Set the moderation log channel")
    @is_admin_or_mod()
    async def setlogchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await db.update_guild_setting(interaction.guild.id, log_channel_id=channel.id)
        await interaction.response.send_message(f"✅ Moderation logs will go to {channel.mention}.")

    # ---------- CHAT TRIGGER CONFIG ----------
    @app_commands.command(name="setchattrigger", description="Configure when Lucy jumps into chat")
    @is_admin_or_mod()
    @app_commands.choices(mode=[
        app_commands.Choice(name="Only when mentioned/replied to", value="mention"),
        app_commands.Choice(name="Dedicated channel only", value="channel"),
        app_commands.Choice(name="Dedicated channel + mentions everywhere else", value="channel_or_mention"),
        app_commands.Choice(name="Anywhere her name is said or she's pinged", value="name"),
        app_commands.Choice(name="All of the above", value="all"),
    ])
    async def setchattrigger(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        await db.update_guild_setting(interaction.guild.id, chat_trigger_mode=mode.value)
        await interaction.response.send_message(f"✅ Lucy's chat trigger mode set to **{mode.name}**.")

    @app_commands.command(name="setchatchannel", description="Set the dedicated channel for chatting with Lucy")
    @is_admin_or_mod()
    async def setchatchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await db.update_guild_setting(interaction.guild.id, chat_channel_id=channel.id)
        await interaction.response.send_message(f"✅ Lucy will actively chat in {channel.mention}.")

    @app_commands.command(name="setventchannel", description="Set the vent channel Lucy quietly watches over")
    @is_admin_or_mod()
    async def setventchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await db.set_vent_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(
            f"✅ Lucy will keep an eye on {channel.mention} and quietly flag the owner if someone seems "
            "like they need a check-in. She won't reply publicly there unless mentioned."
        )

    @app_commands.command(name="setchannelawareness", description="Toggle Lucy's verbal channel-redirection hints")
    @is_admin_or_mod()
    async def setchannelawareness(self, interaction: discord.Interaction, enabled: bool):
        await db.update_guild_setting(interaction.guild.id, channel_redirection_enabled=enabled)
        await interaction.response.send_message(
            f"✅ Channel-redirection hints are now {'on' if enabled else 'off'}."
        )

    @app_commands.command(name="setidlechatter", description="Toggle Lucy's low-key idle chatter in quiet chat channels")
    @is_admin_or_mod()
    async def setidlechatter(self, interaction: discord.Interaction, enabled: bool):
        await db.update_guild_setting(interaction.guild.id, idle_chatter_enabled=enabled)
        await interaction.response.send_message(
            f"✅ Idle chatter is now {'on' if enabled else 'off'}."
        )

    @app_commands.command(name="disableventchannel", description="Turn off Lucy's vent-channel watcher (no redeploy needed)")
    @is_admin_or_mod()
    async def disableventchannel(self, interaction: discord.Interaction):
        settings = await db.get_guild_settings(interaction.guild.id)
        if not settings.get("vent_channel_id"):
            await interaction.response.send_message("There's no vent channel set right now — nothing to disable.", ephemeral=True)
            return
        await db.update_guild_setting(interaction.guild.id, vent_channel_id=None)
        await interaction.response.send_message(
            "✅ Vent-channel watching is off. Use `/setventchannel` any time to turn it back on."
        )

    @app_commands.command(name="ventstatus", description="Check whether Lucy's vent-channel watcher is on")
    @is_admin_or_mod()
    async def ventstatus(self, interaction: discord.Interaction):
        settings = await db.get_guild_settings(interaction.guild.id)
        channel_id = settings.get("vent_channel_id")
        if not channel_id:
            await interaction.response.send_message("Vent-channel watching is currently **off**.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(channel_id)
        name = channel.mention if channel else f"channel {channel_id} (not found — may have been deleted)"
        await interaction.response.send_message(f"Vent-channel watching is currently **on**, watching {name}.", ephemeral=True)

    # ---------- ROLE ASSIGN ----------
    @app_commands.command(name="giverole", description="Give a role to a member")
    @is_admin_or_mod()
    async def giverole(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        await member.add_roles(role)
        await interaction.response.send_message(f"✅ Gave {role.mention} to {member.mention}.")

    @app_commands.command(name="removerole", description="Remove a role from a member")
    @is_admin_or_mod()
    async def removerole(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        await member.remove_roles(role)
        await interaction.response.send_message(f"✅ Removed {role.mention} from {member.mention}.")

    # ---------- SIMPLE TICKETS ----------
    @app_commands.command(name="ticket", description="Open a private support ticket")
    async def ticket(self, interaction: discord.Interaction):
        guild = interaction.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        channel = await guild.create_text_channel(
            name=f"ticket-{interaction.user.name}", overwrites=overwrites
        )
        await channel.send(f"🎫 {interaction.user.mention} a staff member will be with you shortly!")
        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)

    @app_commands.command(name="closeticket", description="Close the current ticket channel")
    async def closeticket(self, interaction: discord.Interaction):
        if not interaction.channel.name.startswith("ticket-"):
            await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
            return
        await interaction.response.send_message("🔒 Closing ticket in 5 seconds...")
        await interaction.channel.delete(delay=5)

    # ---------- INFO ----------
    @app_commands.command(name="serverinfo", description="Show info about this server")
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        embed = discord.Embed(title=guild.name, color=discord.Color.blurple())
        embed.add_field(name="Members", value=guild.member_count)
        embed.add_field(name="Created", value=guild.created_at.strftime("%Y-%m-%d"))
        embed.add_field(name="Owner", value=str(guild.owner))
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="userinfo", description="Show info about a member")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        embed = discord.Embed(title=str(member), color=discord.Color.blurple())
        embed.add_field(name="Joined", value=member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "Unknown")
        embed.add_field(name="Account created", value=member.created_at.strftime("%Y-%m-%d"))
        roles = ", ".join(r.mention for r in member.roles if r.name != "@everyone")
        embed.add_field(name="Roles", value=roles or "None", inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))