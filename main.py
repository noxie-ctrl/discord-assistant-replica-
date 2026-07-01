import os
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv

from utils import database as db

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_PREFIX = os.getenv("DEFAULT_PREFIX", "!")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix=DEFAULT_PREFIX, intents=intents, help_command=None)

COGS = ["cogs.moderation", "cogs.utility", "cogs.personality", "cogs.ai_chat"]


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"⚠️ Slash sync failed: {e}")
    await bot.change_presence(activity=discord.Game(name="managing the server | /help"))


@bot.command(name="help")
async def help_command(ctx: commands.Context):
    embed = discord.Embed(
        title="💁‍♀️ Lucy's Commands",
        description="Most commands work as both `/slash` and prefix commands.",
        color=discord.Color.pink(),
    )
    embed.add_field(
        name="Moderation",
        value="`ban` `kick` `mute` `unmute` `unban` `warn` `warnings` `purge`",
        inline=False,
    )
    embed.add_field(
        name="Utility",
        value="`setwelcome` `setlogchannel` `setchattrigger` `setchatchannel` "
              "`giverole` `removerole` `ticket` `closeticket` `serverinfo` `userinfo`",
        inline=False,
    )
    embed.add_field(
        name="Personality",
        value="`setpersonality` `profile` `resetpersonality`",
        inline=False,
    )
    embed.add_field(
        name="Chat",
        value="Just mention me, reply to me, say my name, or chat in my dedicated channel "
              "(configure with `/setchattrigger`)!",
        inline=False,
    )
    await ctx.send(embed=embed)


async def main():
    await db.init_db()
    async with bot:
        for cog in COGS:
            await bot.load_extension(cog)
        await bot.start(TOKEN)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    asyncio.run(main())
