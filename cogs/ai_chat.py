"""
cogs/ai_chat.py

Listens for messages per each guild's configured chat trigger (mention /
dedicated channel / name-said / all), builds a grounded prompt, and replies
via NVIDIA NIM.

New in this version:
  - Every incoming message updates the sender's user_profiles row (message
    count, last_seen) via utils.database.touch_profile.
  - The sender's long-term notes are pulled in and injected into the system
    prompt so Lucy "remembers" people across sessions, not just the last 24
    messages in a channel.
  - If the sender is the bot owner, is_owner=True is passed through to
    nim_client.build_system_prompt, which layers in the personal-assistant-
    with-a-secret-crush persona.
  - Every 15 messages from a given user, a lightweight side-call summarizes
    recent chat into 2-4 durable facts and updates their notes.
  - Mentioned-user fact injection (join date, account age, roles) is kept
    from the previous fix.
"""

import re
import logging

import discord
from discord.ext import commands

from utils import database as db
from utils import nim_client

logger = logging.getLogger("lucy.ai_chat")

NOTES_UPDATE_INTERVAL = 15  # messages between long-term memory refreshes


class AIChat(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _strip_self_mention(self, content: str) -> str:
        if self.bot.user:
            content = content.replace(f"<@{self.bot.user.id}>", "").replace(
                f"<@!{self.bot.user.id}>", ""
            )
        return content.strip()

    async def _resolve_other_mentions(self, message: discord.Message) -> tuple[str, list[str]]:
        """Replace non-Lucy mentions with @DisplayName in the text, and build
        a list of hard-fact strings about each mentioned user."""
        content = message.content
        facts: list[str] = []

        for user in message.mentions:
            if self.bot.user and user.id == self.bot.user.id:
                continue
            display = user.display_name
            content = re.sub(rf"<@!?{user.id}>", f"@{display}", content)

            member = message.guild.get_member(user.id) if message.guild else None
            if member:
                roles = [r.name for r in member.roles if r.name != "@everyone"]
                joined = member.joined_at.strftime("%B %d, %Y") if member.joined_at else "unknown"
                created = member.created_at.strftime("%B %d, %Y")
                facts.append(
                    f"{display} (@{user.name}) joined this server on {joined}, their Discord "
                    f"account was created on {created}, and their roles are: "
                    f"{', '.join(roles) if roles else 'none'}."
                )

        return content.strip(), facts

    async def _should_respond(self, message: discord.Message) -> bool:
        if message.guild is None or message.author.bot:
            return False

        settings = await db.get_guild_settings(message.guild.id)
        trigger = settings.get("chat_trigger") or "mention"

        if trigger == "all":
            return True
        if trigger == "mention":
            return self.bot.user in message.mentions
        if trigger == "channel":
            return message.channel.id == settings.get("chat_channel_id")
        if trigger == "name":
            personality = await db.get_personality(message.guild.id)
            name = (personality.get("name") or "lucy").lower()
            return name in message.content.lower()
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not await self._should_respond(message):
            return

        async with message.channel.typing():
            await self._handle_chat(message)

    async def _handle_chat(self, message: discord.Message):
        guild = message.guild
        author = message.author

        # 1. Long-term profile bookkeeping
        profile = await db.touch_profile(guild.id, author.id, author.display_name)

        if profile["message_count"] % NOTES_UPDATE_INTERVAL == 0:
            history = await db.get_chat_history(guild.id, message.channel.id, limit=NOTES_UPDATE_INTERVAL)
            recent_from_user = [
                h["content"] for h in history
                if h["role"] == "user" and h["speaker_id"] == author.id
            ]
            if recent_from_user:
                new_notes = await nim_client.summarize_user_notes(
                    author.display_name, recent_from_user, profile.get("notes") or ""
                )
                if new_notes != profile.get("notes"):
                    await db.update_profile_notes(guild.id, author.id, new_notes)
                    profile["notes"] = new_notes

        # 2. Resolve mentions of other users into real facts, strip Lucy's own mention
        cleaned_content, mentioned_facts = await self._resolve_other_mentions(message)
        cleaned_content = self._strip_self_mention(cleaned_content)
        if not cleaned_content:
            cleaned_content = "(no text — attachment or empty mention)"

        # 3. Store the incoming message in short-term channel memory, tagged with speaker
        await db.add_chat_message(
            guild.id, message.channel.id, author.id, author.display_name, "user", cleaned_content
        )

        # 4. Build grounded system prompt
        personality = await db.get_personality(guild.id)
        owner_id = getattr(self.bot, "owner_id", None)
        is_owner = owner_id is not None and author.id == owner_id
        owner_member = guild.owner  # real owner, not "whoever the chat jokes about"
        owner_name = owner_member.display_name if owner_member else "the server owner"

        system_prompt = nim_client.build_system_prompt(
            personality=personality,
            guild_name=guild.name,
            owner_name=owner_name,
            is_owner=is_owner,
            speaker_notes=profile.get("notes") or None,
            mentioned_users_facts=mentioned_facts or None,
        )

        history = await db.get_chat_history(guild.id, message.channel.id, limit=24)
        chat_messages = [{"role": "system", "content": system_prompt}]
        for h in history:
            role = "assistant" if h["role"] == "assistant" else "user"
            prefix = f"{h['speaker_name']}: " if role == "user" and h["speaker_name"] else ""
            chat_messages.append({"role": role, "content": f"{prefix}{h['content']}"})

        # 5. Call the model
        try:
            reply = await nim_client.call_nim(chat_messages)
        except Exception:
            logger.exception("NIM call failed for guild %s channel %s", guild.id, message.channel.id)
            await message.reply(
                "Sorry, I'm having trouble thinking straight right now — try again in a bit?",
                mention_author=False,
            )
            return

        # 6. Store assistant reply, then send (split if too long for Discord)
        await db.add_chat_message(guild.id, message.channel.id, None, None, "assistant", reply)

        for chunk_start in range(0, len(reply), 2000):
            await message.channel.send(reply[chunk_start:chunk_start + 2000])


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChat(bot))