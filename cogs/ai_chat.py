import discord
from discord.ext import commands

from utils import database as db
from utils.nim_client import get_ai_reply


class AIChat(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _should_respond(self, message: discord.Message, settings: dict) -> bool:
        mode = settings.get("chat_trigger_mode", "mention")
        bot_user = self.bot.user
        name = (settings.get("bot_name") or "lucy").lower()

        is_mentioned = bot_user in message.mentions
        is_reply_to_bot = (
            message.reference is not None
            and message.reference.resolved is not None
            and getattr(message.reference.resolved, "author", None) == bot_user
        )
        is_name_said = "lucy" in message.content.lower()
        is_dedicated_channel = message.channel.id == settings.get("chat_channel_id")

        if mode == "mention":
            return is_mentioned or is_reply_to_bot
        elif mode == "channel":
            return is_dedicated_channel
        elif mode == "name":
            return is_mentioned or is_reply_to_bot or is_name_said
        elif mode == "all":
            return is_mentioned or is_reply_to_bot or is_name_said or is_dedicated_channel
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        settings = await db.get_guild_settings(message.guild.id)
        if not self._should_respond(message, settings):
            return

        content = message.content
        for mention in message.mentions:
            content = content.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
        content = content.strip()
        if not content:
            content = "Hey!"

        speaker_name = message.author.display_name
        tagged_content = f"{speaker_name}: {content}"

        owner = message.guild.owner
        if owner is None and message.guild.owner_id:
            try:
                owner = await message.guild.fetch_member(message.guild.owner_id)
            except discord.HTTPException:
                owner = None
        owner_name = owner.display_name if owner else "unknown"

        async with message.channel.typing():
            profile = await db.get_personality(message.guild.id)
            history = await db.get_chat_memory(message.guild.id, message.channel.id)
            reply = await get_ai_reply(
                profile, history, tagged_content,
                guild_name=message.guild.name, owner_name=owner_name,
            )

            await db.add_chat_memory(message.guild.id, message.channel.id, "user", tagged_content)
            await db.add_chat_memory(message.guild.id, message.channel.id, "assistant", reply)

        # Split long replies to respect Discord's 2000 char limit
        for chunk in [reply[i:i + 1900] for i in range(0, len(reply), 1900)]:
            await message.reply(chunk, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChat(bot))