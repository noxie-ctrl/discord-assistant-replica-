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

Max Awareness, Phase 1 (this session): new lookup_member tool (see
MAX_AWARENESS_HANDOFF.md) — on-demand info about any guild member, not just
ones who've chatted with her, merged into every tool list unconditionally
(no permission gate, same treatment as CONCERN_TOOLS). format_member_lookup()
is the pure formatter, next to maybe_suggest_channel_redirection above.

Idle chatter (this session): now fans out to a per-guild list of channels
(idle_chatter_channels table) instead of a single hardcoded chat_channel_id,
via resolve_idle_chatter_channel_ids() below — a guild that hasn't added any
channels explicitly still falls back to the old chat_channel_id behavior.
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
from utils import facts
from utils import github_client
from utils import persona_engine

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

# -----------------------------------------------------------------
# Reply-length fix: a hard, code-level ceiling on top of the prompt's own
# "match the room's energy" guidance, same belt-and-suspenders pattern as
# strip_roleplay_formatting() in nim_client.py for formatting. Prompt-only
# guidance was getting ignored often enough (casual messages coming back as
# full paragraphs) that this adds a real cap, not just an instruction.
# -----------------------------------------------------------------
CASUAL_MAX_TOKENS = 220
DEEP_MAX_TOKENS = 700
_DEPTH_LONG_MESSAGE_CHAR_THRESHOLD = 140
_DEPTH_SIGNAL_PHRASES = [
    "explain", "why does", "why is", "why do", "why did", "how does", "how do",
    "how did", "breakdown", "break down", "in detail", "elaborate",
    "difference between", "walk me through", "what do you think about",
    "thoughts on", "critique", "review", "feedback on", "advice", "advise",
    "help me understand", "compare", "pros and cons", "step by step",
    "steps to", "tutorial", "guide to", "analysis", "opinion on", "recommend",
    "what's the best way", "whats the best way", "kaise hota", "kyu hota",
    "kaise kare", "samjha do", "samjhao",
]
DEPTH_STEERING_NOTES = {
    "casual": (
        "This message reads as ordinary casual chat — reply the way a person would text back: "
        "1-2 short sentences, no paragraph breaks, no multi-part breakdown, unless what they're "
        "actually asking genuinely can't be answered that briefly."
    ),
    "deep": (
        "This message is actually asking for real depth (an explanation, critique, breakdown, or "
        "advice) — it's fine to write a fuller, multi-paragraph reply here since the topic "
        "genuinely needs the space. Still write it as normal human paragraphs, not bullet points "
        "or bolded headers, unless they explicitly asked for a list."
    ),
}


def classify_reply_depth(content: str) -> str:
    """Pure classifier (unit-testable, no discord mocking needed): decide
    whether an incoming message is asking for real depth (explanation,
    critique, breakdown, advice) or is ordinary casual chat. Drives both
    max_tokens (see CASUAL_MAX_TOKENS/DEEP_MAX_TOKENS) and a steering note
    folded into the prompt for this turn. Returns "deep" or "casual"."""
    text = (content or "").strip().lower()
    if not text:
        return "casual"
    if any(phrase in text for phrase in _DEPTH_SIGNAL_PHRASES):
        return "deep"
    if len(text) >= _DEPTH_LONG_MESSAGE_CHAR_THRESHOLD:
        return "deep"
    return "casual"


# -----------------------------------------------------------------
# Vision fix: image URLs can come from the message itself, from whatever
# it's replying to, or (fallback) from a recent nearby message in the same
# channel — see AIChat._collect_image_urls below for how these combine.
# These two extraction helpers are pure/duck-typed so they're unit-testable
# against simple fakes instead of real discord.Attachment/discord.Embed.
# -----------------------------------------------------------------
IMAGE_FILE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")
IMAGE_HISTORY_LOOKBACK = 6  # how many prior channel messages to scan as a fallback
IMAGE_HISTORY_MAX_AGE_SECONDS = 15 * 60  # only consider images posted recently


def _looks_like_image(content_type: Optional[str], filename: str) -> bool:
    if content_type and content_type.startswith("image/"):
        return True
    return (filename or "").lower().endswith(IMAGE_FILE_EXTENSIONS)


def extract_image_urls_from_attachments(attachments) -> list[str]:
    """attachments: any iterable of objects with .url/.content_type/.filename
    (real discord.Attachment objects in production, simple namespaces in
    tests)."""
    urls = []
    for att in attachments or []:
        url = getattr(att, "url", None)
        if not url:
            continue
        if _looks_like_image(getattr(att, "content_type", None), getattr(att, "filename", "")):
            urls.append(url)
    return urls


def extract_image_urls_from_embeds(embeds) -> list[str]:
    """Covers the case of a pasted image link that Discord auto-embeds
    rather than a native attachment. embeds: any iterable of objects with
    .image.url / .thumbnail.url (real discord.Embed in production, simple
    namespaces in tests)."""
    urls = []
    for embed in embeds or []:
        image = getattr(embed, "image", None)
        image_url = getattr(image, "url", None) if image else None
        if image_url:
            urls.append(image_url)
            continue
        thumbnail = getattr(embed, "thumbnail", None)
        thumb_url = getattr(thumbnail, "url", None) if thumbnail else None
        if thumb_url:
            urls.append(thumb_url)
    return urls


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


def resolve_idle_chatter_channel_ids(
    configured_channel_ids: list[int], fallback_channel_id: Optional[int]
) -> list[int]:
    """Pure helper (unit-testable without mocking discord.Guild): given a
    guild's explicitly-configured idle-chatter channels (idle_chatter_channels
    table, set via /addidlechatterchannel) and the legacy single
    chat_channel_id, return the channel ids idle chatter should actually
    consider this pass.

    If the admin has explicitly configured one or more channels, use only
    those — an explicit list means they've taken over channel selection, so
    we don't also silently include the old single-channel default. If
    nothing's been explicitly configured yet, fall back to the old
    chat_channel_id behavior so a guild that hasn't touched the new commands
    doesn't lose idle chatter on upgrade."""
    if configured_channel_ids:
        return list(configured_channel_ids)
    if fallback_channel_id:
        return [fallback_channel_id]
    return []


def format_member_lookup(data: dict) -> str:
    """Pure formatter for the lookup_member tool (Max Awareness, Phase 1) —
    takes a plain dict of already-extracted fields so it's unit-testable
    without mocking discord.Member/discord.Guild objects. The discord-object
    extraction that builds this dict lives in _execute_tool_call, same as
    the rest of that function (thin, not unit tested directly).

    Expected keys: display_name, username, is_bot, joined, created, roles,
    notes (all optional except display_name/is_bot).

    Deliberately no status/activity fields yet — see INFO_TOOLS in
    utils/nim_client.py for why (Presence intent not enabled yet)."""
    display_name = data.get("display_name") or "that member"
    username = data.get("username")
    handle = f" (@{username})" if username else ""

    # Bot check comes first and short-circuits everything else — this is
    # the cheap, reliable half of "distinguish bot vs. member" that Max
    # Awareness asked for, and it's easy to get backwards by accident if
    # this isn't the very first thing checked.
    if data.get("is_bot"):
        return f"{display_name}{handle} is a bot account, not a person."

    joined = data.get("joined") or "unknown"
    created = data.get("created") or "unknown"
    roles = data.get("roles") or "none"

    parts = [
        f"{display_name}{handle} — joined this server on {joined}, Discord "
        f"account created {created}, roles: {roles}."
    ]
    notes = data.get("notes")
    if notes:
        parts.append(f"Known notes: {notes}")
    return " ".join(parts)


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

            configured = await db.get_idle_chatter_channels(guild.id)
            channel_ids = resolve_idle_chatter_channel_ids(
                configured, settings.get("chat_channel_id")
            )

            for channel_id in channel_ids:
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

        if name == "lookup_member":
            # Unlike assign_role/create_role above, this has no permission
            # gate — it's always in the tool list (see INFO_TOOLS in
            # nim_client.py) because it only surfaces info any member could
            # already see by clicking a profile.
            member_name = (args.get("member_name") or "").strip().lower()
            if not member_name:
                return "Error: no member name given."
            target = discord.utils.find(
                lambda m: m.display_name.lower() == member_name or m.name.lower() == member_name,
                guild.members,
            )
            if target is None:
                return f"Error: no member named '{member_name}' found."

            profile = await db.get_profile(guild.id, target.id)
            notes = profile.get("notes") if profile else None

            data = {
                "display_name": target.display_name,
                "username": target.name,
                "is_bot": target.bot,
                "joined": target.joined_at.strftime("%B %d, %Y") if target.joined_at else None,
                "created": target.created_at.strftime("%B %d, %Y") if target.created_at else None,
                "roles": ", ".join(r.name for r in target.roles if r.name != "@everyone") or "none",
                "notes": notes,
            }
            return format_member_lookup(data)

        if name == "get_weather":
            location = (args.get("location") or "").strip()
            if not location:
                return "Error: no location given."
            return await facts.get_weather(location)

        if name == "search_fact":
            query = (args.get("query") or "").strip()
            if not query:
                return "Error: no query given."
            return await facts.search_fact(query)

        if name == "search_github_activity":
            repo = (args.get("repo") or "").strip().lower() or None
            days = args.get("days") or 7
            try:
                days = max(1, min(int(days), 30))
            except (TypeError, ValueError):
                days = 7
            activity = await db.get_recent_github_activity(guild.id, repo=repo, days=days)
            if not activity:
                scope = f"'{repo}'" if repo else "any linked repo"
                return f"No GitHub activity found for {scope} in the last {days} day(s)."
            lines = []
            for item in activity:
                if item["kind"] == "commits":
                    lines.append(f"[{item['repo']}] Commits: {item['title']}")
                else:
                    lines.append(f"[{item['repo']}] PR #{item['ref']} \"{item['title']}\" — {item['detail'] or 'no summary'}")
            return "\n".join(lines)

        if name in ("get_repo_overview", "search_repo_code", "read_repo_file"):
            return await self._execute_github_repo_tool(name, args, guild)

        return f"Error: unknown tool '{name}'."

    async def _resolve_linked_repo(self, guild: discord.Guild, repo_arg: str | None) -> tuple[dict | None, str]:
        """Matches a (possibly omitted) repo argument against this guild's
        linked repos. Returns (link_row_or_None, error_message) — exactly
        one of which is populated, so callers can just check link first."""
        links = await db.list_github_links(guild.id)
        if not links:
            return None, "Error: no GitHub repos are linked in this server (use /githublink first)."

        if repo_arg:
            wanted = repo_arg.strip().lower()
            match = next((l for l in links if l["repo"] == wanted), None)
            if not match:
                available = ", ".join(l["repo"] for l in links)
                return None, f"Error: '{repo_arg}' isn't linked in this server. Linked repos: {available}."
            return match, ""

        if len(links) == 1:
            return links[0], ""

        available = ", ".join(l["repo"] for l in links)
        return None, f"Error: multiple repos are linked — specify one of: {available}."

    async def _execute_github_repo_tool(self, name: str, args: dict, guild: discord.Guild) -> str:
        repo_arg = (args.get("repo") or "").strip().lower() or None
        link, error = await self._resolve_linked_repo(guild, repo_arg)
        if not link:
            return error

        owner, repo_name = link["repo"].split("/", 1)
        branch = link["default_branch"] or "main"

        if name == "get_repo_overview":
            readme = await github_client.get_readme(owner, repo_name)
            tree = await github_client.get_repo_tree(owner, repo_name, branch)
            parts = [f"Repo: {link['repo']} (default branch: {branch})"]
            if readme:
                parts.append("README (may be truncated):\n" + readme[:3000])
            else:
                parts.append("No README found.")
            if tree:
                parts.append("Top-level structure:\n" + "\n".join(tree[:150]))
            return "\n\n".join(parts)

        if name == "search_repo_code":
            query = (args.get("query") or "").strip()
            if not query:
                return "Error: no search query given."
            try:
                results = await github_client.search_code(owner, repo_name, query)
            except github_client.GitHubError as e:
                return f"Error: {e}"
            if not results:
                return f"No code matches for '{query}' in {link['repo']}."

            chunks = [f"Search results for '{query}' in {link['repo']}:"]
            for r in results:
                chunks.append(f"- {r['path']}")
            # Pull excerpts for the top couple of matches so the model can
            # actually answer, not just list filenames.
            for r in results[:2]:
                content = await github_client.get_file_content(owner, repo_name, r["path"], branch)
                if content:
                    chunks.append(f"\n--- {r['path']} (excerpt) ---\n{content[:2000]}")
            return "\n".join(chunks)

        if name == "read_repo_file":
            path = (args.get("path") or "").strip().lstrip("/")
            if not path:
                return "Error: no file path given."
            content = await github_client.get_file_content(owner, repo_name, path, branch)
            if content is None:
                return f"Error: couldn't read '{path}' in {link['repo']} — it may not exist, be a directory, or be a binary file."
            return f"{path} in {link['repo']}:\n{content}"

        return f"Error: unknown tool '{name}'."

    # -----------------------------------------------------------------
    # Main chat handling
    # -----------------------------------------------------------------

    async def _collect_image_urls(self, message: discord.Message) -> list[str]:
        """Gather image URLs relevant to this message, in priority order:
        1. Attachments/embeds on the message itself (the direct "here's an
           image, @lucy" case — worked before, unchanged).
        2. Whatever message this one is a *reply* to — covers "someone
           posts an image, someone else hits Reply and asks @lucy about
           it," which previously found nothing because only the current
           message's attachments were ever checked.
        3. A short recent-history fallback in the same channel — covers
           "someone posts an image, a different person just says '@lucy
           thoughts on this' without formally replying to it." Capped to a
           handful of very recent messages so it can't drag in an
           unrelated image from earlier in a busy channel.
        """
        urls = extract_image_urls_from_attachments(message.attachments)
        urls += extract_image_urls_from_embeds(message.embeds)
        if urls:
            return urls[:3]

        if message.reference is not None:
            referenced = message.reference.resolved
            if referenced is None or isinstance(referenced, discord.DeletedReferencedMessage):
                try:
                    referenced = await message.channel.fetch_message(message.reference.message_id)
                except (discord.NotFound, discord.HTTPException):
                    referenced = None
            if referenced is not None:
                urls = extract_image_urls_from_attachments(referenced.attachments)
                urls += extract_image_urls_from_embeds(referenced.embeds)
                if urls:
                    return urls[:3]

        try:
            now = discord.utils.utcnow()
            async for earlier in message.channel.history(limit=IMAGE_HISTORY_LOOKBACK, before=message):
                age_seconds = (now - earlier.created_at).total_seconds()
                if age_seconds > IMAGE_HISTORY_MAX_AGE_SECONDS:
                    break  # history() is newest-first, so anything older only gets older
                found = extract_image_urls_from_attachments(earlier.attachments)
                found += extract_image_urls_from_embeds(earlier.embeds)
                if found:
                    return found[:3]
        except discord.HTTPException:
            logger.debug("Image history fallback scan failed for channel %s", message.channel.id)

        return []

    async def _maybe_attach_image_context(self, message: discord.Message, chat_messages: list[dict]) -> None:
        if not openrouter_client.is_configured():
            return
        urls = await self._collect_image_urls(message)
        if not urls:
            return
        try:
            description = await openrouter_client.describe_images(urls)
        except Exception as e:
            # Bug fixed here: this used to just log and return, leaving the
            # model with zero awareness an image ever existed — which it
            # then "resolved" by confidently inventing a story ("just an
            # empty mention, no attachment") that sounded plausible but was
            # flatly wrong, since a real image WAS there and the vision call
            # simply failed (rate limit / timeout). Telling it the truth
            # here — image existed, couldn't be read, technical issue —
            # keeps that failure mode from turning into a fresh hallucination.
            logger.warning("Image description failed: %s", e)
            chat_messages.append({
                "role": "system",
                "content": (
                    "An image WAS attached to this message, but it couldn't be analyzed just now "
                    "(a technical hiccup on the vision service, not a missing attachment). Do NOT "
                    "say there was no image/attachment or invent what it might contain — say "
                    "plainly you couldn't quite load/see it and ask them to resend if it matters."
                ),
            })
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
        is_first_message_ever = profile["message_count"] == 1

        # Adaptive persona (utils/persona_engine.py): load this person's
        # current style axes once per message (cheap — already part of the
        # touch_profile row, no extra query) so build_system_prompt below
        # always has a value, even between the periodic update passes.
        style_profile, style_confidence = persona_engine.load_profile_row(profile)

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

                # Same cadence, same batch of messages — blend a free
                # lexical read with one small LLM read into this person's
                # style profile. Anyone who never touches /vibecheck still
                # gets adapted to, just more gradually.
                heuristic_deltas = persona_engine.heuristic_signal(recent_from_user)
                if heuristic_deltas:
                    style_profile, style_confidence = persona_engine.apply_heuristic_deltas(
                        style_profile, style_confidence, heuristic_deltas
                    )
                inferred_deltas = await nim_client.infer_style_signals(author.display_name, recent_from_user)
                if inferred_deltas:
                    style_profile, style_confidence = persona_engine.apply_inferred_deltas(
                        style_profile, style_confidence, inferred_deltas
                    )
                if heuristic_deltas or inferred_deltas:
                    await db.save_style_profile(guild.id, author.id, style_profile, style_confidence)

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
        adaptation_note = persona_engine.render_adaptation_layer(style_profile, style_confidence)

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
            adaptation_note=adaptation_note,
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

        # 5c. Reply-length fix: classify casual-vs-deep for THIS message and
        # fold in a steering note + a matching max_tokens ceiling, rather
        # than relying only on the static prompt's "match the room's
        # energy" guidance (see CASUAL_MAX_TOKENS/DEEP_MAX_TOKENS above).
        depth = classify_reply_depth(cleaned_content)
        max_tokens = DEEP_MAX_TOKENS if depth == "deep" else CASUAL_MAX_TOKENS
        chat_messages.append({"role": "system", "content": DEPTH_STEERING_NOTES[depth]})

        await self._maybe_attach_image_context(message, chat_messages)

        # 6. Call the model (semaphore-capped), with tool support
        try:
            async with self._nim_semaphore:
                tools = (
                    nim_client.CONCERN_TOOLS + nim_client.INFO_TOOLS + nim_client.GROUNDING_TOOLS
                    + (nim_client.TOOLS if can_use_tools else [])
                )
                assistant_message = await nim_client.call_nim_with_tools(
                    chat_messages, tools=tools, max_tokens=max_tokens
                )

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
                        final_message = await nim_client.call_nim_with_tools(
                            chat_messages, tools=None, max_tokens=max_tokens
                        )
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

        # First-ever message from this person in this guild: offer a fast,
        # skippable calibration so Lucy can start adapting sooner than
        # passive inference alone would. Purely a fast-track — passive
        # adaptation (above) runs regardless of whether they ever touch this.
        if is_first_message_ever and not profile.get("onboarded_at"):
            try:
                await message.channel.send(
                    f"(hey {author.display_name} — `/vibecheck` if you want, 4 taps and I'll "
                    "match your vibe faster. totally optional, I'll pick it up naturally either way)",
                    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=False),
                )
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChat(bot))