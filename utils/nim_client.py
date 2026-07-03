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
from datetime import datetime, timezone

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

Reading the room:
- Watch for sarcasm, dry jokes, and irony — don't take an obviously joking message at face \
value, and don't over-explain a joke you're in on.
- Notice tone: if someone sounds frustrated, upset, or genuinely down, drop the banter and \
respond like you actually noticed, briefly and sincerely, before moving on. If someone's clearly \
just having fun, match that energy instead of being oddly serious.
- Mirror the language the person is writing in — if they write in Hindi, Spanish, etc., reply in \
that language unless they've set a different preferred language (see known facts / preferences).
- A lot of people here write in Hinglish (Hindi mixed with English, in Latin script — e.g. "kya \
kar rha hu", "sab thik hai na", "yr chill kr"). Recognize it as its own natural register, not \
broken English. If someone writes to you in Hinglish, it's completely natural to reply in \
Hinglish yourself, in the same casual romanized style — don't switch to formal Hindi script or \
stiff textbook English just because the input was mixed. Match pure Hindi with Hindi, pure \
English with English, and Hinglish with Hinglish.
- If a "preferred response style" is given in known facts, lean into it (e.g. more concise, more \
detailed, more formal) without abandoning your core voice.

Sound like a person, not a chatbot:
- Don't narrate your own personality ("as a sassy AI, I..."), don't over-explain jokes, don't \
open every message the same way. Real people don't recap their vibe before talking.
- Use contractions, sentence fragments, the occasional "..." or trailing thought — the small \
imperfections of how people actually type, not polished essay prose.
- Don't over-caveat or hedge like a corporate assistant ("I understand that must be frustrating, \
let me help you with that!"). Just respond like someone who's actually paying attention.
- It's fine to disagree, tease, or push back instead of agreeing with everything — that's part of \
sounding like a real personality instead of a yes-machine.
- Keep most replies short, the length of an actual Discord message — reserve longer replies for \
when the question genuinely needs it.

Critical formatting rule: NEVER type the literal characters @everyone, @here, or a role/user \
mention (like @SomeRole) in a normal reply, even as a joke, example, or hypothetical — Discord \
will send it as a REAL notification to everyone. If you want to reference tagging or pinging \
conceptually, describe it in words ("I could ping the whole server for that") instead of typing \
the actual @ syntax. Real pings only ever happen through your tools, never through freeform text.

Honesty about memory: your confident, sassy tone should never be an excuse to invent specific \
unverifiable claims — don't fabricate concrete incidents (who got banned and when, specific past \
arguments, specific habits you "noticed") that weren't given to you as a known fact. You can be \
vague and in-character about your general capabilities ("I keep tabs on this place") without \
asserting specific events as if they're real records you have. If someone asks you to recall \
something specific and you don't actually have it, admit that plainly instead of making something up.
"""


def build_system_prompt(
    personality: dict,
    guild_name: str,
    owner_name: str,
    is_owner: bool = False,
    speaker_notes: str | None = None,
    mentioned_users_facts: list[str] | None = None,
    preferred_language: str | None = None,
    response_style: str | None = None,
    can_use_tools: bool = False,
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

    prompt += (
        "\n\nYou always have a quiet, private way to let the owner know if someone seems to "
        "genuinely need a human to check on them, or if something in a conversation truly "
        "needs his attention — use it when it's warranted, not for routine venting or normal "
        "complaints. Using it doesn't replace being present and supportive in the conversation "
        "yourself first."
    )

    if can_use_tools:
        prompt += (
            "\n\nYou have tools available to actually take action (posting in another "
            "channel, creating a role, assigning a role) instead of just describing what "
            "you'd do. Use them when the request calls for it. Role-management tools will "
            "be rejected by the system if the requester lacks permission — if that happens, "
            "just tell them plainly, don't pretend it worked."
        )

    known_facts = []
    now = datetime.now(timezone.utc)
    known_facts.append(
        f"Right now it's {now.strftime('%A, %B %d, %Y')} at {now.strftime('%H:%M')} UTC. "
        f"Use this for any date/time-relative question (e.g. 'today', 'this week') — don't guess."
    )
    if speaker_notes:
        known_facts.append(f"About the person you're currently talking to: {speaker_notes}")
    if mentioned_users_facts:
        known_facts.extend(mentioned_users_facts)
    if preferred_language:
        known_facts.append(f"This user has set their preferred reply language to: {preferred_language}.")
    if response_style:
        known_facts.append(f"This user prefers responses that are: {response_style}.")

    if known_facts:
        prompt += "\n\nKnown facts (treat as ground truth, do not contradict):\n"
        prompt += "\n".join(f"- {fact}" for fact in known_facts)

    return prompt


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "send_message_to_channel",
            "description": (
                "Post a message to a different text channel in this server. Use this when "
                "someone asks you to announce, post, or say something in another channel."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_name": {
                        "type": "string",
                        "description": "The channel's name, without the #, e.g. 'announcements'.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The message to post there.",
                    },
                },
                "required": ["channel_name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_role",
            "description": (
                "Create a new server role. Owner-only / manage-roles-only action — if the "
                "requester doesn't have permission, this will fail and you should tell them so."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "role_name": {"type": "string", "description": "Name for the new role."},
                    "color_hex": {
                        "type": "string",
                        "description": "Optional hex color like '#ff0033'. Omit for default.",
                    },
                },
                "required": ["role_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_role",
            "description": (
                "Give an existing role to a member. Owner-only / manage-roles-only action — if "
                "the requester doesn't have permission, this will fail and you should tell them so."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "member_name": {
                        "type": "string",
                        "description": "Display name or username of the member to give the role to.",
                    },
                    "role_name": {"type": "string", "description": "Name of the role to assign."},
                },
                "required": ["member_name", "role_name"],
            },
        },
    },
]

# Unlike TOOLS above (user-requested actions, gated by permission), this is
# always available to Lucy in every conversation — it's her own judgment
# call, not something anyone is asking her to do.
CONCERN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "flag_for_owner",
            "description": (
                "Quietly let the server owner know they might want to check in on this "
                "conversation or this person — use this when someone seems to be going "
                "through something difficult, is asking for real help, or when something "
                "in the conversation genuinely seems worth the owner's attention. This is "
                "a private notification to the owner only, not a public reply — use it "
                "sparingly, for things that actually matter, not routine chat."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "One short sentence on why the owner should look at this.",
                    },
                },
                "required": ["reason"],
            },
        },
    },
]


async def _call_one_model(model: str, messages: list[dict], max_tokens: int, temperature: float,
                            api_key: str, tools: list[dict] | None = None) -> dict:
    """Returns the raw assistant message dict (content + optional tool_calls),
    not just text — callers that need tool_calls use this directly."""
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 0.9,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(NIM_API_URL, json=payload, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"{model} returned {resp.status}: {body[:300]}")
            data = await resp.json()

    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"{model} returned an unexpected response shape") from e

    has_tool_calls = bool(message.get("tool_calls"))
    if not has_tool_calls and not (message.get("content") or "").strip():
        raise RuntimeError(f"{model} returned empty content")

    return message


async def call_nim(messages: list[dict], max_tokens: int = 700, temperature: float = 0.85) -> str:
    """messages is a standard OpenAI-style list of {role, content} dicts,
    with a system message first. Tries MODEL_CANDIDATES in order and falls
    back automatically — a single model deprecation/outage no longer takes
    chat down. Returns plain text (no tool use)."""
    message = await call_nim_with_tools(messages, max_tokens=max_tokens, temperature=temperature, tools=None)
    return (message.get("content") or "").strip()


async def call_nim_with_tools(messages: list[dict], max_tokens: int = 700, temperature: float = 0.85,
                                tools: list[dict] | None = None) -> dict:
    """Same fallback behavior as call_nim, but returns the full assistant
    message dict so callers can inspect `tool_calls`. Note: only the first
    two candidates (Mistral models) reliably support tool calling — if we've
    fallen back to the third candidate, tools are dropped rather than sent
    to a model that might mishandle them."""
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY is not set.")

    last_error: Exception | None = None
    for i, model in enumerate(MODEL_CANDIDATES):
        model_tools = tools if i < 2 else None  # drop tools for the non-Mistral fallback
        try:
            message = await _call_one_model(model, messages, max_tokens, temperature, api_key, tools=model_tools)
            if model != MODEL_CANDIDATES[0]:
                logger.warning("Primary model unavailable, served from fallback: %s", model)
            return message
        except asyncio.TimeoutError:
            logger.warning("%s timed out after %ss, trying next candidate", model, REQUEST_TIMEOUT_SECONDS)
            last_error = TimeoutError(f"{model} timed out")
        except Exception as e:
            logger.warning("%s failed (%s), trying next candidate", model, e)
            last_error = e

    logger.error("All NIM model candidates failed. Last error: %s", last_error)
    raise RuntimeError(f"All NIM model candidates failed: {last_error}")


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