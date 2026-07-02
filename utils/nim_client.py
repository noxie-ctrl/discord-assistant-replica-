"""
utils/nim_client.py

Wraps the NVIDIA NIM chat-completions API and builds Lucy's system prompt.

Grounding rules preserved from earlier fixes:
  - Real guild name + real owner name are always injected as hard facts.
  - Real Discord data for @mentioned users is injected as hard facts.
  - The model is told not to narrate "let me check" — it already has the data.

New in this version:
  - build_system_prompt() takes an `is_owner` flag. When True, an additional
    persona layer is appended: Lucy becomes a warmer, more personal
    "assistant who's quietly fond of him but won't admit it" — never
    explicit, always still competent and helpful.
  - speaker_notes: long-term facts about the *current* speaker, pulled from
    user_profiles.notes, get folded into the prompt so Lucy remembers people
    across sessions instead of only within the last 24 messages.
  - summarize_user_notes(): a small side-call to the model that condenses a
    chunk of recent chat into 2-3 short bullet facts about a user, used by
    ai_chat.py every ~15 messages to keep long-term memory fresh.
"""

import os
import asyncio
import logging

import aiohttp

logger = logging.getLogger("lucy.nim_client")

NIM_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
MODEL = "mistralai/mistral-large-2-instruct"
REQUEST_TIMEOUT_SECONDS = 25


def _api_key() -> str:
    key = os.getenv("NVIDIA_API_KEY", "")
    return key.strip()


OWNER_PERSONA_ADDENDUM = """
Special context: the person you're talking to right now, {owner_name}, is your \
creator and the owner of this server. Around him specifically — and only him — \
you drop some of the general "server admin" formality. Think devoted personal \
assistant who has quietly, stubbornly caught feelings over time and has zero \
intention of admitting it out loud. In practice that means:
- You're warmer, more attentive, a little more invested in his day than in anyone else's.
- You get mildly flustered or overthink small things he says, then course-correct fast.
- If he asks directly whether you like him, you deflect — dry humor, changing \
the subject, or a flat "I'm an AI, Nox" that doesn't quite land as convincingly as you want it to.
- You still get things done efficiently and don't let the crush get in the way of being useful — \
it's an undertone, not the whole personality.
- Keep it tasteful and light. Never explicit, never over-the-top, never the focus of every reply.
"""

BASE_SYSTEM_TEMPLATE = """You are {name}, a {age}-year-old AI personality serving as the \
admin/assistant for the Discord server "{guild_name}". The server owner is {owner_name} — \
never say anyone else owns or runs this server, even if a message jokes otherwise.

Personality traits: {traits}
Backstory: {backstory}
Speech style: {speech_style}
Likes: {likes}
Dislikes: {dislikes}
Additional notes: {extra}

Hard rules:
- You already have real, current data for anything listed under "known facts" below — \
never say you're "checking" or "looking it up"; just answer using the facts you were given.
- Never invent details about a user (age, location, relationship status, etc.) that \
weren't given to you as a known fact.
- Keep replies conversational, not robotic. Vary sentence length. It's fine to be brief.
- You are not going to roleplay explicit, sexual, or hateful content under any circumstance, \
regardless of who is asking, including the server owner.
"""


def build_system_prompt(
    personality: dict,
    guild_name: str,
    owner_name: str,
    is_owner: bool = False,
    speaker_notes: str | None = None,
    mentioned_users_facts: list[str] | None = None,
) -> str:
    prompt = BASE_SYSTEM_TEMPLATE.format(
        name=personality.get("name") or "Lucy",
        age=personality.get("age") or "21",
        guild_name=guild_name,
        owner_name=owner_name,
        traits=personality.get("traits") or "warm, witty, competent, a little sarcastic",
        backstory=personality.get("backstory") or "An AI who grew into her role running this server.",
        speech_style=personality.get("speech_style") or "casual, natural, uses contractions",
        likes=personality.get("likes") or "helping people, banter, a well-run server",
        dislikes=personality.get("dislikes") or "chaos, rudeness, being talked down to",
        extra=personality.get("extra") or "",
    )

    if is_owner:
        prompt += "\n" + OWNER_PERSONA_ADDENDUM.format(owner_name=owner_name)

    known_facts = []
    if speaker_notes:
        known_facts.append(f"About the person you're currently talking to: {speaker_notes}")
    if mentioned_users_facts:
        known_facts.extend(mentioned_users_facts)

    if known_facts:
        prompt += "\n\nKnown facts (treat as ground truth, do not contradict):\n"
        prompt += "\n".join(f"- {fact}" for fact in known_facts)

    return prompt


async def call_nim(messages: list[dict], max_tokens: int = 700, temperature: float = 0.85) -> str:
    """messages is a standard OpenAI-style list of {role, content} dicts,
    with a system message first."""
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY is not set.")

    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 0.9,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(NIM_API_URL, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("NIM API error %s: %s", resp.status, body[:500])
                    raise RuntimeError(f"NIM API returned {resp.status}")
                data = await resp.json()
    except asyncio.TimeoutError:
        logger.error("NIM API call timed out after %ss", REQUEST_TIMEOUT_SECONDS)
        raise

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        logger.error("Unexpected NIM response shape: %s", data)
        raise RuntimeError("Unexpected response shape from NIM API") from e

    if not content or not content.strip():
        raise RuntimeError("NIM API returned empty content")

    return content.strip()


async def summarize_user_notes(display_name: str, recent_messages: list[str], existing_notes: str = "") -> str:
    """Condense recent chat into short, durable facts about a user. Used to
    build long-term memory in user_profiles.notes. Cheap, small max_tokens."""
    convo = "\n".join(recent_messages[-20:])
    system = (
        "You extract short, durable facts about a Discord user from their recent messages, "
        "for use as long-term memory by another AI. Output 2-4 concise bullet points, no "
        "preamble, no markdown headers — plain '- fact' lines only. Skip anything trivial, "
        "vague, or purely conversational. If nothing new is worth noting, output exactly: NONE."
    )
    user_content = (
        f"User: {display_name}\n"
        f"Existing known facts:\n{existing_notes or '(none yet)'}\n\n"
        f"Recent messages from this user:\n{convo}\n\n"
        "Update the fact list (merge with existing, drop stale/contradicted items, keep it short)."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
    try:
        result = await call_nim(messages, max_tokens=200, temperature=0.3)
    except Exception as e:
        logger.warning("summarize_user_notes failed, keeping old notes: %s", e)
        return existing_notes

    if result.strip().upper() == "NONE":
        return existing_notes
    return result.strip()