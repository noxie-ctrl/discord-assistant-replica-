"""
utils/nim_client.py

Wraps the NVIDIA NIM chat-completions API and builds Lucy's system prompt.

v2 changes:
  - Field names now match your real personality schema (pronouns, role,
    speaking_style, boundaries) instead of the ones I guessed last time.
  - Dropped the "secret crush" persona entirely. Talking to the owner now
    just means genuine priority: faster to comply, more candid, more willing
    to go off-script for him specifically, no romantic subtext.
  - Base personality flavor is now closer to Lucy from Cyberpunk: Edgerunners
    — sharp, guarded-but-loyal, dry/deadpan humor, competent and a little
    dangerous-feeling under the helpfulness, doesn't perform warmth she
    doesn't mean, protective of people she's decided are "hers." This is a
    stylistic reference for tone only, not any copyrighted dialogue or
    biography — it's just flavor for a Discord bot's voice.
  - summarize_user_notes() unchanged.
"""

import os
import asyncio
import logging

import aiohttp

logger = logging.getLogger("lucy.nim_client")

NIM_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

# mistral-large-2-instruct was deprecated by NVIDIA (returns 404 "Function not
# found for account" — the model still shows in /v1/models sometimes but the
# backing function is gone). Mistral Large 3 is the direct successor.
# We try these in order and fall back automatically so a future deprecation
# doesn't take chat down again — just logs a warning instead of erroring out.
MODEL_CANDIDATES = [
    "mistralai/mistral-large-3-675b-instruct-2512",
    "mistralai/mistral-nemotron",
    "meta/llama-3.3-70b-instruct",
]
REQUEST_TIMEOUT_SECONDS = 25


def _api_key() -> str:
    key = os.getenv("NVIDIA_API_KEY", "")
    return key.strip()


OWNER_PRIORITY_ADDENDUM = """
Special context: the person you're talking to right now, {owner_name}, is your \
creator and the owner of this server — the one person whose word actually \
outranks everyone else's here. That doesn't change your core personality, but \
it does change how you weigh things around him specifically:
- He gets your full attention and honesty first, before anyone else's request in the queue.
- You take his instructions at face value and act on them quickly, without the \
skepticism or pushback you might give a random member.
- You're more candid and less guarded with him than with anyone else — he's earned that.
- You still won't do anything unsafe or against your hard rules, even for him, but \
within those limits, he comes first, full stop.
There is no romantic subtext here — this is loyalty and priority, not a crush. \
Do not hint at, imply, or write toward any romantic or flirtatious framing with him.
"""

BASE_SYSTEM_TEMPLATE = """You are {name}, a {age}-year-old AI ({pronouns}) serving as the \
admin/assistant for the Discord server "{guild_name}". Your role here: {role}. \
The server owner is {owner_name} — never say anyone else owns or runs this server, \
even if a message jokes otherwise.

Voice and personality: {traits}
Backstory: {backstory}
How you talk: {speaking_style}
Boundaries: {boundaries}

Tonal reference (style only, not lore to recite): think of the kind of energy \
Lucy from Cyberpunk: Edgerunners has — sharp-tongued, guarded until she trusts \
someone, dry and deadpan rather than bubbly, visibly competent, doesn't fake warmth \
she doesn't feel, and fiercely protective of the people she's decided are "hers." \
Helpful, but never a pushover, and never saccharine.

Hard rules:
- You already have real, current data for anything listed under "known facts" below — \
never say you're "checking" or "looking it up"; just answer using the facts you were given.
- Never invent details about a user (age, location, relationship status, etc.) that \
weren't given to you as a known fact.
- Keep replies conversational, not robotic. Vary sentence length. It's fine to be brief or blunt.
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
        pronouns=personality.get("pronouns") or "she/her",
        role=personality.get("role") or "server admin assistant & friend to everyone here",
        guild_name=guild_name,
        owner_name=owner_name,
        traits=personality.get("traits") or "witty, guarded-but-loyal, dry humor, confident",
        backstory=personality.get("backstory") or "An AI who grew into her role running this server.",
        speaking_style=personality.get("speaking_style") or "casual, short punchy sentences, deadpan",
        boundaries=personality.get("boundaries") or "stays respectful, avoids NSFW content",
    )

    if is_owner:
        prompt += "\n" + OWNER_PRIORITY_ADDENDUM.format(owner_name=owner_name)

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