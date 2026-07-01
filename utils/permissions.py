import discord
from discord import app_commands
from discord.ext import commands


def is_admin_or_mod():
    """Allows users with Administrator or Manage Server/Kick/Ban permissions."""
    def predicate(interaction: discord.Interaction) -> bool:
        perms = interaction.user.guild_permissions
        return perms.administrator or perms.kick_members or perms.ban_members or perms.manage_guild
    return app_commands.check(predicate)


def is_admin_or_mod_ctx():
    async def predicate(ctx: commands.Context) -> bool:
        perms = ctx.author.guild_permissions
        return perms.administrator or perms.kick_members or perms.ban_members or perms.manage_guild
    return commands.check(predicate)
