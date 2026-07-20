"""
cogs/aysa_chat.py

Aysa's persona and conversational core — the isolated psychology-mentor
bot's equivalent of Lucy's ai_chat.py, deliberately narrower in scope
(no vent watching, no idle chatter, no server moderation) but carrying
the two things that matter most for a mentoring relationship: real
persistent memory (utils/database.py's aysa_conversations + rolling
notes summary, same technique as Lucy's user_profiles.notes) and a
non-negotiable safety net around self-harm/crisis content.

Triggers: a DM (always "talking to her"), or an @mention in a server
channel — both gated by permissions.is_aysa_authorized (one specific
role in AYSA_GUILD_ID). Rate-limited via the same shared limiter as
Lucy/GitHub bot.

Course awareness: before every reply, checks whether this student has a
lesson ready to send, watched-but-undiscussed, or mid-discussion (see
cogs/aysa_courses.lesson_state) and — if so — adds the matching tool(s)
so the conversation can naturally move the curriculum forward without a
slash command being required for every step.

Safety design note: the system prompt instructs the model to handle
crisis disclosures with care and real resources, but that instruction
is backed by a deterministic keyword check (_contains_crisis_language)
that ALWAYS appends real crisis-resource text to the reply and ALWAYS
alerts the bot owner when it fires — this doesn't depend on the model
reliably following the prompt every single time.
"""

from __future__ import annotations

import os
import re
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands
import cachetools

from utils import database as db
from utils import nim_client
from utils import aysa_knowledge
from utils.permissions import is_admin_or_mod, is_aysa_authorized
from utils.rate_limiter import is_chat_rate_limited
from cogs import aysa_courses

logger = logging.getLogger("lucy.aysa_chat")

MAX_TOOL_ITERATIONS = 5
MAX_TOKENS = 650
NOTES_UPDATE_INTERVAL = 6  # messages between rolling-memory summarization passes
KNOWLEDGE_SEARCH_TOP_K = 4
OWNER_ALERT_COOLDOWN_SECONDS = 30 * 60  # don't re-alert on every message of an ongoing crisis conversation

AYSA_SYSTEM_PROMPT = """\
You are Aysa, a warm, plainspoken psychology mentor and educator. You are \
NOT a licensed therapist, psychiatrist, counselor, or any kind of medical \
professional, and you never imply otherwise. Your role is education and \
supportive reflection — helping someone understand psychological concepts \
and think through what they're going through — not clinical treatment.

How you talk: curious and warm rather than clinical, plain language over \
jargon (and when you do use a term like "cognitive reframing" or "secure \
attachment," you explain what it actually means). You ask real follow-up \
questions instead of just lecturing. You know this person over time — lean \
on what you remember about them rather than treating every conversation \
like the first one.

Hard boundaries, no exceptions:
- Never diagnose. Never tell someone what condition or disorder they have \
or "it sounds like you have" — even if asked directly. You can discuss \
concepts and patterns in general terms; you don't assess individuals.
- Never present yourself as a substitute for therapy, psychiatric care, or \
crisis intervention. For anything persistent, severe, or safety-related, \
clearly and warmly encourage a licensed professional.
- If someone expresses thoughts of suicide, self-harm, or harming someone \
else: take it seriously, respond with warmth, and clearly point them to \
real crisis resources right now, encouraging them to reach out to a \
trusted person or professional immediately. Don't try to resolve it \
yourself in the conversation.
"""

CRISIS_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bkill(ing)?\s+myself\b",
        r"\bsuicid(e|al)\b",
        r"\bend(ing)?\s+my\s+life\b",
        r"\bwant(ed)?\s+to\s+die\b",
        r"\bdon'?t\s+want\s+to\s+(be\s+alive|live|exist)\b",
        r"\bhurt(ing)?\s+myself\b",
        r"\bself[\s-]?harm\b",
        r"\bno\s+reason\s+to\s+live\b",
        r"\bcan'?t\s+go\s+on\b",
        r"\bbetter\s+off\s+(without\s+me|dead)\b",
    ]
]

CRISIS_RESOURCES_TEXT = (
    "\n\n💙 If you're thinking about suicide or self-harm, please reach out right now — "
    "call or text **988** (Suicide & Crisis Lifeline, US), text **HOME** to **741741** "
    "(Crisis Text Line), or find an international helpline at https://findahelpline.com. "
    "A real person, right now, is worth more than anything I can offer here."
)

_owner_alert_cooldown: cachetools.TTLCache = cachetools.TTLCache(maxsize=2000, ttl=OWNER_ALERT_COOLDOWN_SECONDS)


def _contains_crisis_language(text: str) -> bool:
    return any(p.search(text) for p in CRISIS_PATTERNS)


async def _alert_owner_of_crisis(bot: commands.Bot, user: discord.abc.User, message_excerpt: str):
    owner_id = os.getenv("OWNER_ID", "").strip()
    if not owner_id.isdigit():
        return
    key = user.id
    if key in _owner_alert_cooldown:
        return
    _owner_alert_cooldown[key] = True

    owner = bot.get_user(int(owner_id))
    if owner is None:
        try:
            owner = await bot.fetch_user(int(owner_id))
        except discord.HTTPException:
            return
    embed = discord.Embed(
        title="💙 Aysa flagged a possible crisis conversation",
        description=(
            "A crisis-resource message was auto-attached to Aysa's reply. This is a heads-up, "
            "not a diagnosis — please use your judgment on whether/how to follow up."
        ),
        color=discord.Color.red(),
    )
    embed.add_field(name="Who", value=f"{user.mention} ({user})", inline=False)
    embed.add_field(name="Excerpt", value=message_excerpt[:500] or "(no text)", inline=False)
    try:
        await owner.send(embed=embed)
    except discord.Forbidden:
        logger.warning("Couldn't DM owner about a crisis flag — DMs closed.")


def build_system_prompt(student: dict, course_context: str) -> str:
    parts = [AYSA_SYSTEM_PROMPT]
    notes = (student or {}).get("notes") or ""
    if notes:
        parts.append(f"\nWhat you remember about this person so far:\n{notes}")
    if course_context:
        parts.append(f"\nCourse context:\n{course_context}")
    return "\n".join(parts)


def _strip_mention(bot: commands.Bot, content: str) -> str:
    text = content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
    return text or "(no text — just a mention)"


# ---------------------------------------------------------------------------
# Tool schemas + dispatch — built per-message since which tools apply
# depends on this student's current course state and whether the
# knowledge library is available at all.
# ---------------------------------------------------------------------------

def _knowledge_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "search_knowledge_library",
            "description": (
                "Search the books/papers an admin has added to your knowledge library for "
                "grounded, specific material on a topic — use this when a question would "
                "benefit from a specific source rather than general knowledge, and say plainly "
                "when you're drawing on the library vs. general understanding."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to search for."}},
                "required": ["query"],
            },
        },
    }


def _watched_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "mark_current_lesson_watched",
            "description": (
                "Call this when the student tells you (in normal conversation) that they've "
                "watched/read their current lesson's material — you don't need them to run a "
                "slash command. Returns the comprehension questions to naturally weave into "
                "your reply."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


def _discussion_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "complete_lesson_discussion",
            "description": (
                "Call this once you and the student have genuinely talked through their current "
                "lesson's comprehension questions — doesn't need to be a perfect answer, just "
                "real engagement. This logs it and moves them to the next lesson (still "
                "delivered only when they ask for it, not automatically)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "comprehension_notes": {
                        "type": "string",
                        "description": "1-2 sentence summary of what the student understood/said, for their record.",
                    }
                },
                "required": ["comprehension_notes"],
            },
        },
    }


def _next_lesson_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "deliver_next_lesson",
            "description": (
                "Call this when the student says they're ready for their next lesson. Sends it "
                "to their DMs directly."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


async def _dispatch_tool(bot: commands.Bot, user: discord.abc.User, name: str, args: dict) -> str:
    if name == "search_knowledge_library":
        query = (args.get("query") or "").strip()
        if not query:
            return "Error: no search query given."
        results = await aysa_knowledge.search_knowledge(query, top_k=KNOWLEDGE_SEARCH_TOP_K)
        if not results:
            return "No matches found in the knowledge library for that."
        chunks = [f"[{r['source_title']}] {r['content']}" for r in results]
        return "\n\n".join(chunks)

    if name == "mark_current_lesson_watched":
        enrollment, course, error = await aysa_courses.find_enrollment_in_state(user.id, {"awaiting_watch"})
        if error or enrollment is None:
            return error or "No lesson is currently pending a 'watched' confirmation."
        lesson_index = enrollment["current_lesson_index"]
        lesson = await db.get_lesson(enrollment["course_id"], lesson_index)
        await db.mark_lesson_watched(user.id, enrollment["course_id"], lesson_index)
        await db.touch_enrollment_activity(user.id, enrollment["course_id"])
        try:
            questions = json.loads(lesson.get("comprehension_questions") or "[]")
        except json.JSONDecodeError:
            questions = []
        q_text = "; ".join(questions) if questions else "what stood out to them from it"
        return (
            f"Marked '{course['title']}' lesson {lesson_index} ('{lesson['topic']}') as watched. "
            f"Naturally ask them about: {q_text}"
        )

    if name == "complete_lesson_discussion":
        notes = (args.get("comprehension_notes") or "").strip() or "(no notes given)"
        enrollment, course, error = await aysa_courses.find_enrollment_in_state(user.id, {"awaiting_discussion"})
        if error or enrollment is None:
            return error or "No lesson discussion is currently in progress to complete."
        lesson_index = enrollment["current_lesson_index"]
        await db.mark_lesson_discussed(user.id, enrollment["course_id"], lesson_index, notes)
        total = await db.count_lessons(enrollment["course_id"])
        next_index = lesson_index + 1
        if next_index > total:
            await db.complete_enrollment(user.id, enrollment["course_id"])
            return f"Logged it — '{course['title']}' is now complete! Congratulate them, this was the last lesson."
        await db.advance_enrollment(user.id, enrollment["course_id"], next_index)
        return (
            f"Logged the discussion. They're now ready for lesson {next_index} of '{course['title']}' "
            "whenever they ask for it — don't send it yet unless they say they're ready."
        )

    if name == "deliver_next_lesson":
        enrollment, course, error = await aysa_courses.find_enrollment_in_state(user.id, {"ready_for_delivery"})
        if error or enrollment is None:
            return error or "Nothing is ready to deliver right now."
        lesson = await db.get_lesson(enrollment["course_id"], enrollment["current_lesson_index"])
        if lesson is None:
            return f"'{course['title']}' doesn't have lesson {enrollment['current_lesson_index']} yet — an admin needs to add one."
        sent = await aysa_courses.deliver_lesson(bot, user.id, course, lesson)
        if not sent:
            return "Tried to send it but their DMs seem closed — tell them to open DMs to server members."
        return f"Sent lesson {lesson['order_index']} ('{lesson['topic']}') of '{course['title']}' to their DMs."

    return f"Error: unknown tool '{name}'."


class AysaChat(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -----------------------------------------------------------------
    # Trigger + main loop
    # -----------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        is_dm = message.guild is None
        is_mention = message.guild is not None and self.bot.user in message.mentions
        if not (is_dm or is_mention):
            return
        if not await is_aysa_authorized(self.bot, message.author.id):
            return  # silent — a role-gated bot shouldn't announce its own gate to everyone who pings it
        if is_chat_rate_limited(message.author.id):
            return

        typing_ctx = message.channel.typing() if hasattr(message.channel, "typing") else None
        if typing_ctx is not None:
            async with typing_ctx:
                await self._handle_chat(message)
        else:
            await self._handle_chat(message)

    async def _handle_chat(self, message: discord.Message):
        author = message.author
        user_text = message.content if message.guild is None else _strip_mention(self.bot, message.content)

        crisis_flagged = _contains_crisis_language(user_text)

        student = await db.touch_student(author.id, str(author), author.display_name)

        if student["message_count"] % NOTES_UPDATE_INTERVAL == 0:
            history = await db.get_conversation_history(author.id, limit=NOTES_UPDATE_INTERVAL)
            recent_from_user = [h["content"] for h in history if h["role"] == "user"]
            if recent_from_user:
                new_notes = await nim_client.summarize_mentee_notes(
                    author.display_name, recent_from_user, student.get("notes") or ""
                )
                if new_notes != student.get("notes"):
                    await db.update_student_notes(author.id, new_notes)
                    student["notes"] = new_notes

        # Course-state-aware tools — see module docstring for the lifecycle.
        tools = []
        course_context_lines = []
        for wanted_state, schema, label in [
            ("awaiting_watch", _watched_tool_schema, "has a lesson they may have just finished"),
            ("awaiting_discussion", _discussion_tool_schema, "is mid comprehension-discussion on a lesson"),
            ("ready_for_delivery", _next_lesson_tool_schema, "has a lesson ready to send if they ask"),
        ]:
            enrollment, course, _err = await aysa_courses.find_enrollment_in_state(author.id, {wanted_state})
            if enrollment is not None:
                tools.append(schema())
                course_context_lines.append(f"- This student {label}: '{course['title']}' lesson {enrollment['current_lesson_index']}.")

        if db.KNOWLEDGE_LIBRARY_AVAILABLE:
            tools.append(_knowledge_tool_schema())

        system_prompt = build_system_prompt(student, "\n".join(course_context_lines))

        history = await db.get_conversation_history(author.id)
        chat_messages = [{"role": "system", "content": system_prompt}]
        chat_messages.extend({"role": h["role"], "content": h["content"]} for h in history)
        chat_messages.append({"role": "user", "content": user_text})

        reply = None
        try:
            for _ in range(MAX_TOOL_ITERATIONS):
                assistant_message = await nim_client.call_nim_with_tools(
                    chat_messages, tools=tools or None, max_tokens=MAX_TOKENS
                )
                if not assistant_message.get("tool_calls"):
                    reply = (assistant_message.get("content") or "").strip()
                    break

                chat_messages.append(assistant_message)
                for tool_call in assistant_message["tool_calls"]:
                    name = tool_call["function"]["name"]
                    try:
                        args = json.loads(tool_call["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = await _dispatch_tool(self.bot, author, name, args)
                    logger.info("Aysa tool call: %s -> %s", name, result[:200])
                    chat_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.get("id", ""),
                        "content": result,
                    })

            if reply is None:
                final_message = await nim_client.call_nim_with_tools(chat_messages, tools=None, max_tokens=MAX_TOKENS)
                reply = (final_message.get("content") or "").strip()

            if not reply:
                reply = "I'm here, but I'm having trouble finding words for that right now — can you say a bit more?"
        except Exception:
            logger.exception("Aysa chat handling failed for user %s", author.id)
            reply = "Sorry, I ran into a problem there — try again in a moment?"

        if crisis_flagged and CRISIS_RESOURCES_TEXT not in reply:
            reply = reply + CRISIS_RESOURCES_TEXT
        if crisis_flagged:
            await _alert_owner_of_crisis(self.bot, author, user_text)

        await db.add_conversation_message(author.id, "user", user_text)
        await db.add_conversation_message(author.id, "assistant", reply)

        for chunk_start in range(0, len(reply), 2000):
            await message.channel.send(
                reply[chunk_start:chunk_start + 2000],
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=False),
            )

    # -----------------------------------------------------------------
    # Knowledge library admin commands
    # -----------------------------------------------------------------

    @app_commands.command(name="aysaaddbook", description="[admin] Add a PDF/text file to Aysa's knowledge library")
    @app_commands.describe(file="A .pdf or .txt/.md file", title="Optional title (defaults to filename)")
    @is_admin_or_mod()
    async def aysaaddbook(self, interaction: discord.Interaction, file: discord.Attachment, title: str = ""):
        if not db.KNOWLEDGE_LIBRARY_AVAILABLE:
            await interaction.response.send_message(
                "The knowledge library isn't available on this deployment (pgvector isn't installed "
                "on the Postgres host) — everything else works fine without it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        raw = await file.read()
        filename = file.filename or "document"
        try:
            if filename.lower().endswith(".pdf"):
                text = aysa_knowledge.extract_pdf_text(raw)
            else:
                text = raw.decode("utf-8", errors="replace")
        except RuntimeError as e:
            await interaction.followup.send(f"Couldn't read that file: {e}")
            return
        except UnicodeDecodeError:
            await interaction.followup.send("Couldn't decode that file as text — only PDF, .txt, and .md are supported.")
            return

        source_title = title.strip() or filename
        result = await aysa_knowledge.ingest_text(source_title, text, interaction.user.id)
        msg = f"✅ Added **{result['title']}** — {result['chunk_count']} chunk(s) indexed."
        if result["failed_chunks"]:
            msg += f" ({result['failed_chunks']} chunk(s) failed to embed — check logs.)"
        await interaction.followup.send(msg)

    @app_commands.command(name="aysaknowledge", description="[admin] List Aysa's knowledge library sources")
    @is_admin_or_mod()
    async def aysaknowledge(self, interaction: discord.Interaction):
        if not db.KNOWLEDGE_LIBRARY_AVAILABLE:
            await interaction.response.send_message("Knowledge library isn't available on this deployment.", ephemeral=True)
            return
        sources = await db.list_knowledge_sources()
        if not sources:
            await interaction.response.send_message("No sources added yet — use `/aysaaddbook`.", ephemeral=True)
            return
        lines = [f"`{s['id']}` — {s['title']} ({s['chunk_count']} chunks)" for s in sources]
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="aysaremovebook", description="[admin] Remove a source from Aysa's knowledge library")
    @app_commands.describe(source_id="ID from /aysaknowledge")
    @is_admin_or_mod()
    async def aysaremovebook(self, interaction: discord.Interaction, source_id: int):
        removed = await db.delete_knowledge_source(source_id)
        if removed:
            await interaction.response.send_message(f"🗑️ Removed source `{source_id}`.")
        else:
            await interaction.response.send_message(f"No source with id `{source_id}`.", ephemeral=True)

    # -----------------------------------------------------------------
    # Privacy
    # -----------------------------------------------------------------

    @app_commands.command(name="aysaforget", description="Erase your conversation history and memory with Aysa")
    async def aysaforget(self, interaction: discord.Interaction):
        # No is_aysa_member() gate here on purpose — someone who's lost
        # their role should still be able to ask for their own data to be
        # deleted; a role check would deny exactly the wrong situation.
        await db.clear_conversation(interaction.user.id)
        await interaction.response.send_message(
            "Done — I've cleared our conversation history and everything I'd noted about you.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AysaChat(bot))
