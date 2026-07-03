"""
cogs/serverlog.py

Tracks member join/leave events and exposes a summary command.
Independent from utility.py's welcome-message listener — discord.py fires
every registered listener for an event, so this doesn't interfere with
existing welcome-message logic, it just also logs to the DB.
"""

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from utils import database as db
from utils.permissions import is_admin_or_mod


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:  # keep it short once we're into multi-day durations
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "<1m"


class ServerLog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await db.record_member_event(member.guild.id, member.id, str(member), "join")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        duration = None
        if member.joined_at:
            duration = int((datetime.now(timezone.utc) - member.joined_at).total_seconds())
        await db.record_member_event(member.guild.id, member.id, str(member), "leave", duration_seconds=duration)

    @app_commands.command(name="serverlog", description="Recent member join/leave activity")
    @is_admin_or_mod()
    @app_commands.describe(limit="How many recent events to show (default 15, max 50)")
    async def serverlog(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 50] = 15):
        events = await db.get_recent_member_events(interaction.guild_id, limit=limit)
        if not events:
            await interaction.response.send_message("No join/leave activity logged yet.")
            return

        lines = []
        for e in events:
            ts = discord.utils.format_dt(e["event_time"], style="R")
            name = e["username"] or f"User {e['user_id']}"
            if e["event_type"] == "join":
                lines.append(f"🟢 **{name}** joined — {ts}")
            else:
                lines.append(f"🔴 **{name}** left — {ts} (was here {_format_duration(e['duration_seconds'])})")

        embed = discord.Embed(
            title=f"Server activity — last {len(events)} events",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerLog(bot))