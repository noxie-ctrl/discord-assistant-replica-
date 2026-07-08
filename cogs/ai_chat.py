"""
cogs/ai_chat.py

v3 changes:
  - Cross-channel continuity: when building context, Lucy also sees the
    speaker's own recent messages from OTHER channels in the guild, not just
    the current one — so switching channels doesn't reset the conversation.
  - Per-channel asyncio.Lock so concurrent messages in the same channel are
    handled one at a time (keeps chat_memory ordering sane under load), plus
    a global semaphore capping concurrent NIM calls so a burst of activity
    across many channels doesn't hammer the API past its rate limit.
  - Real tool-calling: Lucy can post to another channel, create a role, or
    assign a role, gated by Discord permissions — not just talk about doing it.
  - Full mention resolution: users, channels, and roles all get turned into
    readable names (and users additionally get real fact injection), instead
    of only user mentions being handled.
  - User preferences (preferred_language, response_style) are pulled from
    user_profiles and folded into the prompt.
  - Profiles are now keyed with username + display_name + id (not just
    display name), matching the DB schema update.
  - Reaction-based feedback: 👍/👎 on one of Lucy's replies (by the person she
    was replying to) logs to the feedback table.
"""

import re
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from utils import database as db
from utils import nim_client
from utils import groq_client
from utils import awareness
from utils import openrouter_client

logger = logging.getLogger("lucy.ai_chat")

NOTES_UPDATE_INTERVAL = 15  # messages between long-term memory refreshes
IDLE_CHATTER_INTERVAL_SECONDS = 4 * 60 * 60
IDLE_CHATTER_START_HOUR_IST = 8
IDLE_CHATTER_END_HOUR_IST = 23
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_CHANNEL_REDIRECTION_HINTS = {
    "vent": "a vent or support channel",
    "memes": "a memes or fun channel",
    "announcement": "an announcements channel",
    "general": "a general chat channel",
}
CROSS_CHANNEL_CONTEXT_LIMIT = 8
MAX_CONCURRENT_NIM_CALLS = 5
OWNER_ALERT_COOLDOWN_SECONDS = 20 * 60  # don't re-alert about the same person too often
VENT_CHECK_MIN_INTERVAL_SECONDS = 15  # don't classify every single message in a busy vent channel
VENT_MIN_MESSAGE_LENGTH = 12  # skip trivially short messages ("lol", "ok") — not worth a call

FEEDBACK_EMOJI = {"👍": "up", "👎": "down"}


def maybe_suggest_channel_redirection(channel_topic: Optional[str], content: str) -> Optional[str]:
    if not content:
        return None
    text = (content or "").strip().lower()
    if len(text) < 12:
        return None
    if any(token in text for token in ["feel", "hurt", "panic", "suic", "die", "depressed", "alone", "breakdown"]):
        if "meme" in (channel_topic or "").lower() or "fun" in (channel_topic or "").lower():
            return "This sounds like it belongs in a vent or support channel rather than a memes/fun channel."
    if any(token in text for token in ["announcement", "news", "update", "server", "important"]):
        if "announcement" in (channel_topic or "").lower() or "news" in (channel_topic or "").lower():
            return "This looks more like a discussion topic than an announcement post."
    return None


class AIChat(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._channel_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._nim_semaphore = asyncio.Semaphore(MAX_CONCURRENT_NIM_CALLS)
        # message_id -> (original_author_id, guild_id, channel_id, snippet) for feedback tracking
        self._recent_replies: dict[int, tuple[int, int, int, str]] = {}
        # (guild_id, user_id) -> datetime of last owner alert, to avoid spamming
        self._last_alert: dict[tuple[int, int], float] = {}
        # channel_id -> last time we saw a real message there
        self._last_activity_at: dict[int, float] = {}
        # channel_id -> last time we sent idle chatter there
        self._last_idle_chatter_at: dict[int, float] = {}
        # channel_id -> last time we ran a vent-channel classification call
        self._last_vent_check: dict[int, float] = {}

    async def cog_load(self):
        # Prime the news digest once at startup so the very first replies
        # already have it, then keep it refreshed in the background.
        asyncio.create_task(awareness.refresh_digest(force=True))
        self._news_refresh_loop.start()
        self._idle_chatter_loop.start()
        # Same idea, but per-guild and reading the guild's own recent chat
        # instead of RSS (Day 4: server-vibe digest).
        asyncio.create_task(self._refresh_all_guild_vibes(force=True))
        self._server_vibe_refresh_loop.start()

    async def cog_unload(self):
        self._news_refresh_loop.cancel()
        self._idle_chatter_loop.cancel()
        self._server_vibe_refresh_loop.cancel()

    @tasks.loop(hours=3)
    async def _news_refresh_loop(self):
        try:
            await awareness.refresh_digest()
        except Exception:
            logger.exception("Background news digest refresh failed")

    @tasks.loop(hours=6)
    async def _server_vibe_refresh_loop(self):
        try:
            await self._refresh_all_guild_vibes()
        except Exception:
            logger.exception("Background server-vibe refresh failed")

    async def _refresh_all_guild_vibes(self, force: bool = False):
        for guild in self.bot.guilds:
            try:
                settings = await db.get_guild_settings(guild.id)
                if not settings.get("server_vibe_enabled"):
                    continue
                recent = await db.get_recent_guild_messages(
                    guild.id, limit=awareness.SERVER_VIBE_SAMPLE_SIZE
                )
                sample = [r["content"] for r in recent if r.get("content")]
                await awareness.refresh_server_vibe(guild.id, sample, force=force)
            except Exception:
                logger.exception("Server-vibe refresh failed for guild %s", guild.id)

    @tasks.loop(minutes=15)
    async def _idle_chatter_loop(self):
        try:
            await self._maybe_send_idle_chatter()
        except Exception:
            logger.exception("Idle chatter loop failed")

    # -----------------------------------------------------------------
    # Mention resolution
    # -----------------------------------------------------------------

    def _strip_self_mention(self, content: str) -> str:
        if self.bot.user:
            content = content.replace(f"<@{self.bot.user.id}>", "").replace(
                f"<@!{self.bot.user.id}>", ""
            )
        return content.strip()

    async def _resolve_mentions(self, message: discord.Message) -> tuple[str, list[str]]:
        """Turn <@user>, <#channel>, <@&role> into readable names, and build
        a list of hard-fact strings about mentioned users."""
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

        for channel in message.channel_mentions:
            content = re.sub(rf"<#{channel.id}>", f"#{channel.name}", content)

        for role in message.role_mentions:
            content = re.sub(rf"<@&{role.id}>", f"@{role.name}", content)

        return content.strip(), facts

    # -----------------------------------------------------------------
    # Trigger detection
    # -----------------------------------------------------------------

    async def _should_respond(self, message: discord.Message) -> bool:
        if message.guild is None or message.author.bot:
            return False

        settings = await db.get_guild_settings(message.guild.id)
        trigger = settings.get("chat_trigger_mode") or "mention"

        if trigger == "all":
            return True
        if trigger == "mention":
            return self.bot.user in message.mentions
        if trigger == "channel":
            return message.channel.id == settings.get("chat_channel_id")
        if trigger == "channel_or_mention":
            if message.channel.id == settings.get("chat_channel_id"):
                return True
            return self.bot.user in message.mentions
        if trigger == "name":
            personality = await db.get_personality(message.guild.id)
            name = (personality.get("name") or "lucy").lower()
            return name in message.content.lower()
        return False

    # -----------------------------------------------------------------
    # Owner alerts (used by the flag_for_owner tool AND the vent watcher)
    # -----------------------------------------------------------------

    async def _alert_owner(self, message: discord.Message, reason: str):
        owner_id = getattr(self.bot, "owner_id", None)
        if owner_id is None:
            return

        key = (message.guild.id, message.author.id)
        now = asyncio.get_event_loop().time()
        last = self._last_alert.get(key)
        if last is not None and (now - last) < OWNER_ALERT_COOLDOWN_SECONDS:
            return  # already alerted about this person recently, don't spam
        self._last_alert[key] = now

        owner = self.bot.get_user(owner_id) or await self.bot.fetch_user(owner_id)
        if owner is None:
            logger.warning("Could not resolve owner user %s for alert", owner_id)
            return

        embed = discord.Embed(
            title="👀 Might want to check this out",
            description=reason,
            color=discord.Color.orange(),
        )
        embed.add_field(name="Who", value=f"{message.author.mention} ({message.author})", inline=True)
        embed.add_field(name="Where", value=f"#{message.channel.name} in {message.guild.name}", inline=True)
        embed.add_field(name="Message", value=message.content[:500] or "(no text)", inline=False)
        embed.add_field(name="Jump to it", value=message.jump_url, inline=False)

        try:
            await owner.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Couldn't DM owner %s (DMs closed) — alert dropped: %s", owner_id, reason)

    # -----------------------------------------------------------------
    # Passive vent-channel watcher (doesn't require a mention/reply)
    # -----------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return

        self._last_activity_at[message.channel.id] = asyncio.get_event_loop().time()
        will_reply = await self._should_respond(message)

        settings = await db.get_guild_settings(message.guild.id)
        if not will_reply and settings.get("channel_redirection_enabled"):
            channel_topic = getattr(message.channel, "topic", "") or ""
            suggestion = maybe_suggest_channel_redirection(channel_topic, message.content)
            if suggestion and message.channel.id != settings.get("chat_channel_id"):
                asyncio.create_task(self._maybe_redirect_channel(message, suggestion))
        vent_channel_id = settings.get("vent_channel_id")
        # Skip the separate classification pass if this message is about to go
        # through the full chat pipeline anyway — the flag_for_owner tool
        # already covers concern-flagging there, no need to double-call NIM.
        if vent_channel_id and message.channel.id == vent_channel_id and not will_reply:
            asyncio.create_task(self._check_vent_message(message))

        if not will_reply:
            return

        # Serialize handling per channel so rapid-fire messages in the same
        # channel don't interleave and scramble chat_memory ordering.
        lock = self._channel_locks[message.channel.id]
        async with lock:
            async with message.channel.typing():
                await self._handle_chat(message)

    async def _maybe_send_idle_chatter(self):
        now = datetime.now(timezone.utc).astimezone(IST)
        if not (IDLE_CHATTER_START_HOUR_IST <= now.hour <= IDLE_CHATTER_END_HOUR_IST):
            return

        for guild in self.bot.guilds:
            settings = await db.get_guild_settings(guild.id)
            if not settings.get("idle_chatter_enabled"):
                continue
            channel_id = settings.get("chat_channel_id")
            if not channel_id:
                continue
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                continue
            if not channel.permissions_for(guild.me).send_messages:
                continue

            now_ts = asyncio.get_event_loop().time()
            last_activity = self._last_activity_at.get(channel.id, 0.0)
            last_sent = self._last_idle_chatter_at.get(channel.id, 0.0)
            if last_activity and (now_ts - last_activity) < IDLE_CHATTER_INTERVAL_SECONDS:
                continue
            if last_sent and (now_ts - last_sent) < IDLE_CHATTER_INTERVAL_SECONDS:
                continue

            try:
                await channel.send("it's quiet in here today — how's everyone doing?")
                self._last_idle_chatter_at[channel.id] = now_ts
            except discord.HTTPException:
                logger.debug("Idle chatter failed for %s", channel.id)

    async def _maybe_redirect_channel(self, message: discord.Message, suggestion: str):
        try:
            if not message.channel.permissions_for(message.guild.me).send_messages:
                return
            await message.reply(
                f"{suggestion} If you want, I can help you move it to a more fitting channel.",
                mention_author=False,
            )
        except discord.HTTPException:
            logger.debug("Channel redirection hint failed for %s", message.id)

    async def _check_vent_message(self, message: discord.Message):
        """Lightweight, cheap classification pass — does NOT go through the
        full chat pipeline, so it doesn't require a mention and doesn't
        reply publicly. Just quietly decides whether to alert the owner.
        Throttled so a busy vent channel doesn't burn through NIM's rate
        limit — we sample instead of classifying every single message."""
        content = (message.content or "").strip()
        if len(content) < VENT_MIN_MESSAGE_LENGTH:
            return

        now = asyncio.get_event_loop().time()
        last_check = self._last_vent_check.get(message.channel.id)
        if last_check is not None and (now - last_check) < VENT_CHECK_MIN_INTERVAL_SECONDS:
            return
        self._last_vent_check[message.channel.id] = now

        triage_messages = [
            {
                "role": "system",
                "content": (
                    "You triage a single Discord message from a server's vent channel. "
                    "Decide if the person sounds like they're genuinely struggling, upset, "
                    "or going through something hard enough that a trusted adult/friend "
                    "should probably check in on them — as opposed to normal venting, "
                    "complaining, or joking around that doesn't need intervention. "
                    "Reply with exactly one line: YES: <five word reason> or NO."
                ),
            },
            {"role": "user", "content": content[:1000]},
        ]

        # Cheap, high-frequency background task — keep it off the main NIM
        # quota by preferring Groq, falling back to NIM only if Groq isn't
        # configured or has a transient failure.
        classification = None
        if groq_client.is_configured():
            try:
                classification = await groq_client.call_groq(
                    triage_messages, model=groq_client.MODEL_FAST, max_tokens=20, temperature=0.1,
                )
            except Exception as e:
                logger.warning("Groq vent triage failed, falling back to NIM: %s", e)

        if classification is None:
            try:
                classification = await nim_client.call_nim(triage_messages, max_tokens=20, temperature=0.1)
            except Exception as e:
                logger.warning("Vent-channel classification failed, skipping: %s", e)
                return

        if classification.strip().upper().startswith("YES"):
            reason = classification.split(":", 1)[1].strip() if ":" in classification else "Seemed like they could use a check-in."
            await self._alert_owner(message, f"In the vent channel: {reason}")

    # -----------------------------------------------------------------
    # Feedback reactions
    # -----------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
        emoji = str(payload.emoji)
        rating = FEEDBACK_EMOJI.get(emoji)
        if not rating:
            return

        tracked = self._recent_replies.get(payload.message_id)
        if not tracked:
            return
        original_author_id, guild_id, channel_id, snippet = tracked
        if payload.user_id != original_author_id:
            return  # only the person Lucy was replying to can rate it

        await db.add_feedback(guild_id, payload.user_id, channel_id, snippet, rating)
        logger.info("Recorded %s feedback from user %s in guild %s", rating, payload.user_id, guild_id)

    # -----------------------------------------------------------------
    # Tool execution
    # -----------------------------------------------------------------

    async def _execute_tool_call(self, tool_call: dict, message: discord.Message) -> str:
        import json

        name = tool_call["function"]["name"]
        try:
            args = json.loads(tool_call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            return "Error: could not parse tool arguments."

        guild = message.guild
        author = message.author
        owner_id = getattr(self.bot, "owner_id", None)
        is_owner = owner_id is not None and author.id == owner_id
        has_manage_roles = getattr(author.guild_permissions, "manage_roles", False)
        has_manage_guild = getattr(author.guild_permissions, "manage_guild", False)

        if name == "flag_for_owner":
            reason = args.get("reason") or "Lucy flagged this conversation."
            await self._alert_owner(message, reason)
            return "Success: owner has been quietly notified."

        if name == "send_message_to_channel":
            channel_name = (args.get("channel_name") or "").lstrip("#").strip().lower()
            content = args.get("content") or ""
            target = discord.utils.find(
                lambda c: isinstance(c, discord.TextChannel) and c.name.lower() == channel_name,
                guild.channels,
            )
            if target is None:
                return f"Error: no channel named '{channel_name}' found."
            perms = target.permissions_for(guild.me)
            if not perms.send_messages:
                return f"Error: I don't have permission to send messages in #{target.name}."
            await target.send(
                content,
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=False),
            )
            return f"Success: posted to #{target.name}."

        if name == "create_role":
            if not (is_owner or has_manage_roles):
                return "Error: requester doesn't have permission to create roles."
            role_name = args.get("role_name") or "New Role"
            color_hex = args.get("color_hex")
            color = discord.Color.default()
            if color_hex:
                try:
                    color = discord.Color(int(color_hex.lstrip("#"), 16))
                except ValueError:
                    pass
            new_role = await guild.create_role(name=role_name, color=color, reason=f"Requested by {author}")
            return f"Success: created role '{new_role.name}'."

        if name == "assign_role":
            if not (is_owner or has_manage_roles):
                return "Error: requester doesn't have permission to assign roles."
            member_name = (args.get("member_name") or "").lower()
            role_name = (args.get("role_name") or "").lower()
            target_member = discord.utils.find(
                lambda m: m.display_name.lower() == member_name or m.name.lower() == member_name,
                guild.members,
            )
            target_role = discord.utils.find(lambda r: r.name.lower() == role_name, guild.roles)
            if target_member is None:
                return f"Error: no member named '{member_name}' found."
            if target_role is None:
                return f"Error: no role named '{role_name}' found."
            if target_role >= guild.me.top_role:
                return f"Error: I can't assign '{target_role.name}' — it's above my own role."
            await target_member.add_roles(target_role, reason=f"Requested by {author}")
            return f"Success: gave {target_member.display_name} the '{target_role.name}' role."

        return f"Error: unknown tool '{name}'."

    # -----------------------------------------------------------------
    # Main chat handling
    # -----------------------------------------------------------------

    async def _maybe_attach_image_context(self, message: discord.Message, chat_messages: list[dict]) -> None:
        if not openrouter_client.is_configured():
            return
        if not message.attachments:
            return
        if not any(att.content_type and att.content_type.startswith("image/") for att in message.attachments):
            return
        urls = []
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith("image/"):
                urls.append(attachment.url)
        if not urls:
            return
        try:
            description = await openrouter_client.describe_images(urls)
        except Exception as e:
            logger.warning("Image description failed: %s", e)
            return
        if description:
            chat_messages.append({
                "role": "system",
                "content": f"Image context from the message: {description}",
            })

    async def _handle_chat(self, message: discord.Message):
        guild = message.guild
        author = message.author

        # 1. Long-term profile bookkeeping (id + username + display name)
        profile = await db.touch_profile(guild.id, author.id, str(author), author.display_name)

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

        # 2. Resolve mentions (users/channels/roles), strip Lucy's own mention
        cleaned_content, mentioned_facts = await self._resolve_mentions(message)
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
        owner_member = guild.owner
        owner_name = owner_member.display_name if owner_member else "the server owner"

        can_use_tools = is_owner or author.guild_permissions.manage_guild or author.guild_permissions.manage_roles \
            or author.guild_permissions.manage_channels

        relationship_tier = db.get_relationship_tier(profile.get("relationship_score") or 0)

        system_prompt = nim_client.build_system_prompt(
            personality=personality,
            guild_name=guild.name,
            owner_name=owner_name,
            is_owner=is_owner,
            speaker_notes=profile.get("notes") or None,
            mentioned_users_facts=mentioned_facts or None,
            preferred_language=profile.get("preferred_language"),
            response_style=profile.get("response_style"),
            can_use_tools=can_use_tools,
            relationship_tier=relationship_tier,
            news_digest=awareness.get_cached_digest() or None,
            server_vibe=awareness.get_cached_server_vibe(guild.id) or None,
        )

        # 5. In-channel short-term history
        history = await db.get_chat_history(guild.id, message.channel.id, limit=24)
        chat_messages = [{"role": "system", "content": system_prompt}]

        # 5b. Cross-channel continuity — the speaker's own recent messages
        # elsewhere in the guild, so switching channels doesn't lose context.
        cross_channel = await db.get_recent_messages_by_user(
            guild.id, author.id, limit=CROSS_CHANNEL_CONTEXT_LIMIT
        )
        cross_channel = [h for h in cross_channel if h["channel_id"] != message.channel.id]
        if cross_channel:
            summary_lines = "\n".join(f"- {h['content']}" for h in cross_channel[-CROSS_CHANNEL_CONTEXT_LIMIT:])
            chat_messages.append({
                "role": "system",
                "content": (
                    f"For context, here's what {author.display_name} has recently said in OTHER "
                    f"channels in this server (not the current one):\n{summary_lines}"
                ),
            })

        for h in history:
            role = "assistant" if h["role"] == "assistant" else "user"
            prefix = f"{h['speaker_name']}: " if role == "user" and h["speaker_name"] else ""
            chat_messages.append({"role": role, "content": f"{prefix}{h['content']}"})

        await self._maybe_attach_image_context(message, chat_messages)

        # 6. Call the model (semaphore-capped), with tool support
        try:
            async with self._nim_semaphore:
                tools = nim_client.CONCERN_TOOLS + (nim_client.TOOLS if can_use_tools else [])
                assistant_message = await nim_client.call_nim_with_tools(chat_messages, tools=tools)

                if assistant_message.get("tool_calls"):
                    chat_messages.append(assistant_message)
                    for tool_call in assistant_message["tool_calls"]:
                        result = await self._execute_tool_call(tool_call, message)
                        chat_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.get("id", ""),
                            "content": result,
                        })
                    # One more round trip to let Lucy phrase the final reply
                    async with self._nim_semaphore:
                        final_message = await nim_client.call_nim_with_tools(chat_messages, tools=None)
                    reply = (final_message.get("content") or "Done.").strip()
                else:
                    reply = (assistant_message.get("content") or "").strip()

                reply = nim_client.strip_roleplay_formatting(reply, bot_name=personality.get("name", "Lucy"))
        except Exception:
            logger.exception("NIM call failed for guild %s channel %s", guild.id, message.channel.id)
            await message.reply(
                "Sorry, I'm having trouble thinking straight right now — try again in a bit?",
                mention_author=False,
            )
            return

        # 7. Store assistant reply, then send (split if too long for Discord)
        await db.add_chat_message(guild.id, message.channel.id, None, None, "assistant", reply)

        sent_message = None
        for chunk_start in range(0, len(reply), 2000):
            sent_message = await message.channel.send(
                reply[chunk_start:chunk_start + 2000],
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=False),
            )

        if sent_message:
            self._recent_replies[sent_message.id] = (author.id, guild.id, message.channel.id, reply[:200])
            for emoji in FEEDBACK_EMOJI:
                try:
                    await sent_message.add_reaction(emoji)
                except discord.HTTPException:
                    pass
            # keep the tracking dict from growing forever
            if len(self._recent_replies) > 500:
                oldest_key = next(iter(self._recent_replies))
                self._recent_replies.pop(oldest_key, None)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChat(bot))