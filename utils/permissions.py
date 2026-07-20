import os
import logging

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("lucy.permissions")


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


# ---------------------------------------------------------------------------
# Aysa (psychology mentor bot) — single-role gate
# ---------------------------------------------------------------------------
# Aysa only talks to members holding one specific role in one specific
# server (AYSA_GUILD_ID / AYSA_ROLE_ID), whether they reach her via DM or
# an @mention in that server. A DM has no guild/roles of its own, so the
# check always resolves membership through AYSA_GUILD_ID explicitly rather
# than relying on interaction.guild / message.guild.

def _aysa_config() -> tuple[int | None, int | None]:
    guild_id = os.getenv("AYSA_GUILD_ID", "").strip()
    role_id = os.getenv("AYSA_ROLE_ID", "").strip()
    return (
        int(guild_id) if guild_id.isdigit() else None,
        int(role_id) if role_id.isdigit() else None,
    )


async def is_aysa_authorized(bot: commands.Bot, user_id: int) -> bool:
    """True if user_id holds AYSA_ROLE_ID in AYSA_GUILD_ID, or is the bot
    owner (owner always gets through, e.g. for testing). If either env var
    is missing/misconfigured, this fails closed (returns False) rather than
    silently letting everyone in — an unset role gate on a bot built for
    sensitive 1:1 conversations should never default to "open to everyone."
    """
    if _is_owner(user_id):
        return True

    guild_id, role_id = _aysa_config()
    if guild_id is None or role_id is None:
        logger.warning(
            "AYSA_GUILD_ID / AYSA_ROLE_ID not set (or not numeric) — "
            "denying access by default. Set both to let anyone through."
        )
        return False

    guild = bot.get_guild(guild_id)
    if guild is None:
        return False

    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            return False
        except discord.HTTPException:
            logger.warning("Failed to fetch member %s in guild %s for Aysa auth check", user_id, guild_id)
            return False

    return any(role.id == role_id for role in member.roles)


def is_aysa_member():
    """Slash-command check wrapping is_aysa_authorized above — app_commands.check
    accepts a coroutine predicate, so this stays a thin wrapper rather than
    a second implementation of the role logic."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return await is_aysa_authorized(interaction.client, interaction.user.id)
    return app_commands.check(predicate)