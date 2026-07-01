import os
import discord
from discord import app_commands
from discord.ext import commands


def _is_owner(user_id: int) -> bool:
    owner_id = os.getenv("OWNER_ID", "").strip()
    return owner_id.isdigit() and int(owner_id) == user_id


def is_admin_or_mod():
    """Allows the bot owner, or users with Administrator/Manage Server/Kick/Ban permissions."""
    def predicate(interaction: discord.Interaction) -> bool:
        if _is_owner(interaction.user.id):
            return True
        perms = interaction.user.guild_permissions
        return perms.administrator or perms.kick_members or perms.ban_members or perms.manage_guild
    return app_commands.check(predicate)


def is_admin_or_mod_ctx():
    async def predicate(ctx: commands.Context) -> bool:
        if _is_owner(ctx.author.id):
            return True
        perms = ctx.author.guild_permissions
        return perms.administrator or perms.kick_members or perms.ban_members or perms.manage_guild
    return commands.check(predicate)


def is_owner_only():
    """Restricts a command to only the bot owner, regardless of server roles."""
    def predicate(interaction: discord.Interaction) -> bool:
        return _is_owner(interaction.user.id)
    return app_commands.check(predicate)