"""
cogs/aysa_courses.py

Aysa's structured curriculum system — courses, lessons, enrollment,
progress, and the hybrid delivery model she was built for: a background
pass nudges anyone who's gone quiet, but the student always triggers the
actual next step (marking a lesson watched, moving to the next one) —
never a forced push.

Lesson lifecycle per (student, course, lesson_index), tracked in
aysa_lesson_progress — see lesson_state() below:
  1. ready_for_delivery — nothing sent yet for this index
  2. awaiting_watch     — Aysa sent it, student hasn't said they're done
  3. awaiting_discussion — student's done, Aysa's comprehension check-in
                            (her "how'd it land, what did you think" chat)
                            hasn't happened yet
  Only once that discussion actually happens does current_lesson_index
  advance (see cogs/aysa_chat.py's complete_lesson_discussion tool) —
  that's the "like a real teacher, she knows her students" part.

Content for each lesson (a real sourced video/paper + an AI-written
summary and comprehension questions) comes from utils/aysa_content.py,
built once when a lesson is added via /aysaaddlesson — not regenerated
per student.

Lesson delivery always DMs the student directly, regardless of where the
enrollment/next-lesson trigger came from — this is a personal mentoring
relationship, not a public-channel activity feed.
"""

from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import database as db
from utils import aysa_content
from utils.permissions import is_admin_or_mod, is_aysa_member

logger = logging.getLogger("lucy.aysa_courses")

NUDGE_CHECK_INTERVAL_HOURS = int(os.getenv("AYSA_NUDGE_CHECK_INTERVAL_HOURS", "6"))
NUDGE_IDLE_HOURS = int(os.getenv("AYSA_NUDGE_IDLE_HOURS", "48"))
NUDGE_COOLDOWN_HOURS = int(os.getenv("AYSA_NUDGE_COOLDOWN_HOURS", "72"))


# ---------------------------------------------------------------------------
# Shared state helpers — used by this cog's commands, by cogs/aysa_chat.py's
# course-aware tools, and by the nudge loop below.
# ---------------------------------------------------------------------------

async def lesson_state(user_id: int, enrollment: dict) -> tuple[str, dict | None]:
    """Where a student is on their CURRENT lesson for one enrollment.
    Returns (state, progress_row_or_None) — state is one of
    'course_complete', 'ready_for_delivery', 'awaiting_watch',
    'awaiting_discussion'."""
    total = await db.count_lessons(enrollment["course_id"])
    if enrollment["current_lesson_index"] > total:
        return "course_complete", None
    progress = await db.get_lesson_progress(user_id, enrollment["course_id"], enrollment["current_lesson_index"])
    if progress is None or progress.get("delivered_at") is None:
        return "ready_for_delivery", progress
    if progress.get("watched_at") is None:
        return "awaiting_watch", progress
    if progress.get("discussed_at") is None:
        return "awaiting_discussion", progress
    return "awaiting_discussion", progress  # discussed but not yet advanced — transient


async def find_enrollment_in_state(
    user_id: int, wanted_states: set[str], course_hint: str | None = None
) -> tuple[dict | None, dict | None, str]:
    """Finds the (enrollment, course) pair whose current lesson is in one
    of wanted_states, disambiguating by course_hint if more than one
    qualifies — same "specify which one" pattern as
    github_tools.resolve_linked_repo. Returns (enrollment, course, error);
    on a clean 'nothing matches' the error is "" so callers can supply
    their own state-specific message."""
    enrollments = await db.get_active_enrollments_for_user(user_id)
    if not enrollments:
        return None, None, "You're not enrolled in any course yet — see `/aysacourses` and `/aysaenroll`."

    candidates = []
    for e in enrollments:
        state, _ = await lesson_state(user_id, e)
        if state in wanted_states:
            candidates.append(e)

    if not candidates:
        return None, None, ""

    if course_hint:
        wanted = course_hint.strip().lower()
        match = next((e for e in candidates if e["course_title"].lower() == wanted), None)
        if not match:
            available = ", ".join(e["course_title"] for e in candidates)
            return None, None, f"'{course_hint}' isn't in that state right now. Try one of: {available}."
        chosen = match
    elif len(candidates) == 1:
        chosen = candidates[0]
    else:
        available = ", ".join(e["course_title"] for e in candidates)
        return None, None, f"You've got more than one course like that going — which one: {available}?"

    course = await db.get_course(chosen["course_id"])
    return chosen, course, ""


def _lesson_embed(course_title: str, lesson: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"📘 {course_title} — Lesson {lesson['order_index']}: {lesson['topic']}",
        description=(lesson.get("summary") or "(no summary available)")[:4000],
        color=discord.Color.teal(),
    )
    if lesson.get("video_url"):
        embed.add_field(
            name="🎥 Watch", value=f"[{lesson.get('video_title') or 'Video'}]({lesson['video_url']})", inline=False
        )
    if lesson.get("paper_url"):
        embed.add_field(
            name="📄 Read", value=f"[{lesson.get('paper_title') or 'Paper'}]({lesson['paper_url']})", inline=False
        )
    embed.set_footer(text="Whenever you've gone through this, tell me or run /aysadone.")
    return embed


async def deliver_lesson(bot: commands.Bot, user_id: int, course: dict, lesson: dict) -> bool:
    """DMs the lesson and records delivery. Returns False if the DM
    couldn't be sent (DMs closed) so the caller can tell the student to
    open them. Shared by /aysaenroll, /aysanext, and the
    deliver_next_lesson chat tool in cogs/aysa_chat.py."""
    user = bot.get_user(user_id) or await _safe_fetch_user(bot, user_id)
    if user is None:
        return False
    try:
        await user.send(embed=_lesson_embed(course["title"], lesson))
    except discord.Forbidden:
        return False
    await db.mark_lesson_delivered(user_id, course["id"], lesson["order_index"])
    await db.touch_enrollment_activity(user_id, course["id"])
    return True


async def _safe_fetch_user(bot: commands.Bot, user_id: int) -> discord.User | None:
    try:
        return await bot.fetch_user(user_id)
    except discord.NotFound:
        return None
    except discord.HTTPException:
        logger.warning("Failed to fetch user %s", user_id)
        return None


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class AysaCourses(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._nudge_loop.start()

    def cog_unload(self):
        self._nudge_loop.cancel()

    # -----------------------------------------------------------------
    # Admin: course/lesson authoring
    # -----------------------------------------------------------------

    @app_commands.command(name="aysacreatecourse", description="[admin] Create a new Aysa course")
    @app_commands.describe(title="Course title", description="Short description of what it covers")
    @is_admin_or_mod()
    async def aysacreatecourse(self, interaction: discord.Interaction, title: str, description: str = ""):
        existing = await db.find_course_by_title(title)
        if existing:
            await interaction.response.send_message(f"A course called '{title}' already exists.", ephemeral=True)
            return
        course = await db.create_course(title, description, interaction.user.id)
        await interaction.response.send_message(
            f"✅ Created course **{course['title']}** (id {course['id']}). Add lessons with `/aysaaddlesson`."
        )

    @app_commands.command(name="aysaaddlesson", description="[admin] Source and add the next lesson to a course")
    @app_commands.describe(course="Exact course title", topic="What this lesson should teach")
    @is_admin_or_mod()
    async def aysaaddlesson(self, interaction: discord.Interaction, course: str, topic: str):
        course_row = await db.find_course_by_title(course)
        if not course_row:
            await interaction.response.send_message(f"No course called '{course}' — check `/aysacourses`.", ephemeral=True)
            return

        # Sourcing (YouTube + Semantic Scholar search) plus AI summary
        # generation genuinely takes a few seconds — defer so Discord
        # doesn't time out the interaction.
        await interaction.response.defer(thinking=True)
        try:
            material = await aysa_content.source_lesson(topic)
        except Exception:
            logger.exception("Lesson sourcing failed for course %s topic '%s'", course_row["id"], topic)
            await interaction.followup.send("Something went wrong sourcing that lesson — try again in a bit.")
            return

        next_index = await db.count_lessons(course_row["id"]) + 1
        lesson = await db.add_lesson(course_row["id"], next_index, topic, **material)

        parts = [f"✅ Added **Lesson {next_index}: {topic}** to {course_row['title']}."]
        parts.append(f"🎥 Video: {lesson['video_title']}" if lesson.get("video_url") else "🎥 No video found.")
        parts.append(f"📄 Paper: {lesson['paper_title']}" if lesson.get("paper_url") else "📄 No paper found.")
        await interaction.followup.send("\n".join(parts))

    # -----------------------------------------------------------------
    # Student-facing
    # -----------------------------------------------------------------

    @app_commands.command(name="aysacourses", description="List Aysa's available courses")
    @is_aysa_member()
    async def aysacourses(self, interaction: discord.Interaction):
        courses = await db.list_courses()
        if not courses:
            await interaction.response.send_message("No courses have been set up yet.", ephemeral=True)
            return
        embed = discord.Embed(title="📚 Aysa's Courses", color=discord.Color.teal())
        for c in courses:
            lesson_count = await db.count_lessons(c["id"])
            embed.add_field(
                name=c["title"],
                value=f"{c.get('description') or 'No description.'}\n{lesson_count} lesson(s)",
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="aysaenroll", description="Enroll in one of Aysa's courses")
    @app_commands.describe(course="Exact course title (see /aysacourses)")
    @is_aysa_member()
    async def aysaenroll(self, interaction: discord.Interaction, course: str):
        course_row = await db.find_course_by_title(course)
        if not course_row:
            await interaction.response.send_message(f"No course called '{course}' — check `/aysacourses`.", ephemeral=True)
            return
        if await db.count_lessons(course_row["id"]) == 0:
            await interaction.response.send_message("That course doesn't have any lessons yet — check back soon.", ephemeral=True)
            return

        enrollment = await db.enroll_student(interaction.user.id, course_row["id"])
        await interaction.response.send_message(f"🎉 Enrolled in **{course_row['title']}**! Sending lesson 1 to your DMs now.")

        state, _ = await lesson_state(interaction.user.id, enrollment)
        if state != "ready_for_delivery":
            return  # already enrolled + past lesson 1 (re-running the command) — nothing new to send
        lesson = await db.get_lesson(course_row["id"], enrollment["current_lesson_index"])
        sent = await deliver_lesson(self.bot, interaction.user.id, course_row, lesson)
        if not sent:
            await interaction.followup.send(
                "I couldn't DM you the lesson — your DMs might be closed to server members. "
                "Open them up and run `/aysanext` to get it."
            )

    @app_commands.command(name="aysanext", description="Get your next lesson, if you're ready for it")
    @app_commands.describe(course="Optional — only needed if more than one course is ready")
    @is_aysa_member()
    async def aysanext(self, interaction: discord.Interaction, course: str | None = None):
        enrollment, course_row, error = await find_enrollment_in_state(
            interaction.user.id, {"ready_for_delivery"}, course
        )
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        if enrollment is None:
            await interaction.response.send_message(
                "Nothing's ready to send right now — you're either still on your current lesson "
                "or waiting to finish talking one through with me.",
                ephemeral=True,
            )
            return

        lesson = await db.get_lesson(course_row["id"], enrollment["current_lesson_index"])
        if lesson is None:
            await interaction.response.send_message(
                f"'{course_row['title']}' doesn't have lesson {enrollment['current_lesson_index']} yet — "
                "an admin needs to add one.",
                ephemeral=True,
            )
            return

        sent = await deliver_lesson(self.bot, interaction.user.id, course_row, lesson)
        if sent:
            await interaction.response.send_message(f"📬 Sent — check your DMs for lesson {lesson['order_index']}.")
        else:
            await interaction.response.send_message(
                "I couldn't DM you — open your DMs to server members and try again.", ephemeral=True
            )

    @app_commands.command(name="aysadone", description="Tell Aysa you've gone through your current lesson")
    @app_commands.describe(course="Optional — only needed if more than one course is awaiting this")
    @is_aysa_member()
    async def aysadone(self, interaction: discord.Interaction, course: str | None = None):
        enrollment, course_row, error = await find_enrollment_in_state(
            interaction.user.id, {"awaiting_watch"}, course
        )
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        if enrollment is None:
            await interaction.response.send_message(
                "I don't have a lesson marked as sent-but-not-done for you right now — "
                "run `/aysanext` if you're ready for a new one.",
                ephemeral=True,
            )
            return

        lesson_index = enrollment["current_lesson_index"]
        await db.mark_lesson_watched(interaction.user.id, enrollment["course_id"], lesson_index)
        await db.touch_enrollment_activity(interaction.user.id, enrollment["course_id"])
        lesson = await db.get_lesson(enrollment["course_id"], lesson_index)

        questions = []
        if lesson.get("comprehension_questions"):
            try:
                questions = json.loads(lesson["comprehension_questions"])
            except json.JSONDecodeError:
                questions = []

        await interaction.response.send_message(
            f"Nice — marked **{course_row['title']}** lesson {lesson_index} as done. "
            "I sent you a couple of questions in DMs, whenever you want to talk through it."
        )
        user = self.bot.get_user(interaction.user.id) or await _safe_fetch_user(self.bot, interaction.user.id)
        if user is None:
            return
        question_text = "\n".join(f"- {q}" for q in questions) if questions else "What stood out to you from this one?"
        try:
            await user.send(
                f"Glad you got through **lesson {lesson_index}: {lesson['topic']}**. "
                f"A couple things I'm curious about:\n{question_text}\n\n"
                "No rush — just reply whenever, and we'll talk it through."
            )
        except discord.Forbidden:
            pass

    @app_commands.command(name="aysaprogress", description="Show your progress in Aysa's courses")
    @is_aysa_member()
    async def aysaprogress(self, interaction: discord.Interaction):
        enrollments = await db.get_active_enrollments_for_user(interaction.user.id)
        if not enrollments:
            await interaction.response.send_message("You're not enrolled in anything yet — see `/aysacourses`.", ephemeral=True)
            return

        lines = []
        state_labels = {
            "ready_for_delivery": "waiting on the next lesson to be sent",
            "awaiting_watch": "lesson sent, waiting on you",
            "awaiting_discussion": "waiting to talk it through",
            "course_complete": "🎓 completed",
        }
        for e in enrollments:
            state, _ = await lesson_state(interaction.user.id, e)
            total = await db.count_lessons(e["course_id"])
            lines.append(
                f"**{e['course_title']}** — lesson {e['current_lesson_index']}/{total} "
                f"({state_labels.get(state, state)})"
            )
        await interaction.response.send_message("\n".join(lines))

    # -----------------------------------------------------------------
    # Hybrid nudge loop — reminds if idle, never auto-advances anything.
    # -----------------------------------------------------------------

    @tasks.loop(hours=NUDGE_CHECK_INTERVAL_HOURS)
    async def _nudge_loop(self):
        try:
            await self._check_nudges()
        except Exception:
            logger.exception("Aysa nudge loop failed")

    @_nudge_loop.before_loop
    async def _before_nudge_loop(self):
        await self.bot.wait_until_ready()

    async def _check_nudges(self):
        enrollments = await db.list_all_active_enrollments()
        now = datetime.now(timezone.utc)
        for e in enrollments:
            last_activity = e.get("last_activity_at")
            if last_activity is None:
                continue
            if (now - last_activity).total_seconds() / 3600 < NUDGE_IDLE_HOURS:
                continue
            last_nudged = e.get("last_nudged_at")
            if last_nudged is not None and (now - last_nudged).total_seconds() / 3600 < NUDGE_COOLDOWN_HOURS:
                continue

            state, _ = await lesson_state(e["user_id"], e)
            message = self._nudge_message(state, e)
            if message is None:
                continue

            user = self.bot.get_user(e["user_id"]) or await _safe_fetch_user(self.bot, e["user_id"])
            if user is None:
                continue
            try:
                await user.send(message)
                await db.mark_enrollment_nudged(e["user_id"], e["course_id"])
            except discord.Forbidden:
                logger.info("Couldn't nudge user %s (DMs closed)", e["user_id"])

    def _nudge_message(self, state: str, enrollment: dict) -> str | None:
        course_title = enrollment.get("course_title", "your course")
        idx = enrollment["current_lesson_index"]
        if state == "ready_for_delivery":
            return f"Hey — whenever you're ready, lesson {idx} of **{course_title}** is waiting. Just say the word, or run `/aysanext`."
        if state == "awaiting_watch":
            return f"No pressure at all — lesson {idx} of **{course_title}** is still sitting there whenever you get a chance."
        if state == "awaiting_discussion":
            return f"Whenever you want to pick back up, I'd love to hear your thoughts on lesson {idx} of **{course_title}**."
        return None


async def setup(bot: commands.Bot):
    # AysaCourses.get_active_enrollments_for_user joins aysa_courses, so
    # this cog assumes the courses/enrollments schema is already up —
    # true by the time setup_hook runs (db.init_pool() runs first in
    # aysa_bot.py, same ordering as every other cog in this project).
    await bot.add_cog(AysaCourses(bot))
