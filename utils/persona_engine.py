"""
utils/persona_engine.py

Adaptive persona layer: gives Lucy a per-user read on HOW someone likes to
be talked to, separate from relationship_tier (which tracks how CLOSE she
is with them). Two independent dials — a brand-new "acquaintance" can still
want blunt banter, and a "best friend" can still want a gentle touch.

Five axes, each 0-100 (50 = no lean yet), each with its own 0-100
confidence score so a single weird message can't swing behavior:

  directness      gentle/diplomatic (low)   <-> blunt/no-fluff (high)
  banter          sincere/earnest (low)     <-> sarcastic/teasing (high)
  energy          calm/low-key (low)        <-> high-energy/hyped (high)
  depth           quick/light chat (low)    <-> long thoughtful chat (high)
  support_style   wants to be heard (low)   <-> wants solutions (high)

Signal comes in three trust tiers, each with its own weight/confidence-gain
(see apply_explicit_deltas / apply_inferred_deltas / apply_heuristic_deltas):
  - explicit  : /vibecheck answers (cogs/preferences.py) — big, trusted nudge
  - inferred  : small LLM read on a batch of recent messages
                (nim_client.infer_style_signals) — moderate trust
  - heuristic : cheap lexical proxies computed right here, free and instant,
                deliberately low-weight since they're rough proxies, not
                real understanding

This module is deliberately network-free and side-effect-free so all of it
is unit-testable without mocking an API client. The one LLM call
(infer_style_signals) lives in utils/nim_client.py, next to
summarize_user_notes, and calls back into this module only for the AXES
dict (to build its prompt) and parse_inferred_deltas (to validate its
output) — a one-way dependency, nim_client -> persona_engine, no cycle.
"""

import json
import re

# ---------------------------------------------------------------------------
# Axis definitions
# ---------------------------------------------------------------------------

AXES: dict[str, str] = {
    "directness": "gentle/diplomatic (low) vs blunt/no-fluff (high)",
    "banter": "sincere/earnest (low) vs sarcastic/teasing (high)",
    "energy": "calm/low-key (low) vs high-energy/hyped (high)",
    "depth": "prefers quick/light exchanges (low) vs enjoys longer thoughtful ones (high)",
    "support_style": "wants to be heard/validated (low) vs wants solutions/advice (high)",
}

DEFAULT_AXIS_VALUE = 50.0
MIN_CONFIDENCE_TO_RENDER = 22.0


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Signal merging — one generic function, three trust-tier wrappers around it.
# Confidence approaches 100 with diminishing returns (each new signal moves
# it less than the last), so a profile "settles" rather than drifting
# forever on noise.
# ---------------------------------------------------------------------------

def apply_signal(value: float | None, confidence: float | None, delta: float,
                  weight: float, cap_gain: float) -> tuple[float, float]:
    value = DEFAULT_AXIS_VALUE if value is None else value
    confidence = 0.0 if confidence is None else confidence
    new_value = _clamp(value + delta * weight, 0.0, 100.0)
    gain = cap_gain * weight * (1 - confidence / 100.0)
    new_confidence = _clamp(confidence + gain, 0.0, 100.0)
    return new_value, new_confidence


def apply_deltas(profile: dict, confidence: dict, deltas: dict, *,
                  weight: float, cap_gain: float) -> tuple[dict, dict]:
    profile = dict(profile or {})
    confidence = dict(confidence or {})
    for axis, delta in (deltas or {}).items():
        if axis not in AXES or not delta:
            continue
        v, c = apply_signal(profile.get(axis), confidence.get(axis), float(delta), weight, cap_gain)
        profile[axis] = v
        confidence[axis] = c
    return profile, confidence


def apply_explicit_deltas(profile: dict, confidence: dict, deltas: dict) -> tuple[dict, dict]:
    """/vibecheck answers — the user told us directly, trust it fully."""
    return apply_deltas(profile, confidence, deltas, weight=1.0, cap_gain=45.0)


def apply_inferred_deltas(profile: dict, confidence: dict, deltas: dict) -> tuple[dict, dict]:
    """One LLM read on a batch of recent messages — real evidence, but a
    single pass could misjudge, so it moves things at partial trust."""
    return apply_deltas(profile, confidence, deltas, weight=0.6, cap_gain=15.0)


def apply_heuristic_deltas(profile: dict, confidence: dict, deltas: dict) -> tuple[dict, dict]:
    """Free lexical proxies (see heuristic_signal) — rough, so low weight."""
    return apply_deltas(profile, confidence, deltas, weight=0.35, cap_gain=8.0)


# ---------------------------------------------------------------------------
# DB row <-> dict helpers. style_profile/style_confidence are stored as
# plain TEXT (JSON-encoded), matching the rest of user_profiles (e.g.
# `notes`) rather than reaching for asyncpg's JSONB codec setup.
# ---------------------------------------------------------------------------

def load_profile_row(row: dict) -> tuple[dict, dict]:
    """Parses a user_profiles row (as returned by db.touch_profile /
    db.get_profile) into (style_profile, style_confidence) dicts. Missing
    or malformed data degrades to empty dicts rather than raising —
    callers should treat a missing axis as DEFAULT_AXIS_VALUE / 0 confidence."""

    def _parse(raw):
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: float(v) for k, v in data.items() if k in AXES and isinstance(v, (int, float))}

    row = row or {}
    return _parse(row.get("style_profile")), _parse(row.get("style_confidence"))


# ---------------------------------------------------------------------------
# Heuristic (free, instant, lexical) signal
# ---------------------------------------------------------------------------

_EMOJI_PATTERN = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)
_SLANG_WORDS = ("lol", "lmao", "lmfao", "rofl", "bruh", "ong", "ngl", "fr fr", "deadass", "lowkey", "highkey")


def heuristic_signal(messages: list[str]) -> dict[str, float]:
    """Cheap, deterministic lexical proxies — no API call. Deliberately
    only touches energy/banter/depth, the axes that actually show up in
    surface features of text; directness and support_style need real
    reading comprehension and are left entirely to infer_style_signals()."""
    messages = [m for m in (messages or []) if m]
    if not messages:
        return {}

    n = len(messages)
    text = " ".join(messages)
    lower = text.lower()

    avg_len = sum(len(m) for m in messages) / n
    exclaim_rate = text.count("!") / n
    emoji_count = len(_EMOJI_PATTERN.findall(text))
    slang_hits = sum(lower.count(w) for w in _SLANG_WORDS)
    caps_words = sum(1 for w in text.split() if len(w) > 2 and w.isupper())

    deltas: dict[str, float] = {}

    energy_signal = exclaim_rate * 3.0 + caps_words * 0.5 + (emoji_count / n) * 2.0
    if energy_signal:
        deltas["energy"] = _clamp(energy_signal, -6.0, 6.0)

    if slang_hits:
        deltas["banter"] = _clamp(slang_hits * 1.5, -6.0, 6.0)

    depth_signal = (avg_len - 60.0) / 20.0
    if abs(depth_signal) >= 0.5:
        deltas["depth"] = _clamp(depth_signal, -6.0, 6.0)

    return deltas


# ---------------------------------------------------------------------------
# LLM-inferred signal — parsing only (the network call lives in
# nim_client.infer_style_signals, right next to summarize_user_notes).
# ---------------------------------------------------------------------------

def parse_inferred_deltas(raw: str | None) -> dict[str, float] | None:
    """Validates + clamps a model's JSON reply into a clean deltas dict.
    Returns None (not {}) on any failure so callers can distinguish
    "nothing worth noting" from "couldn't parse it" if they ever care to."""
    if not raw:
        return None
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    out = {}
    for axis in AXES:
        v = data.get(axis)
        if isinstance(v, (int, float)) and v:
            out[axis] = _clamp(float(v), -10.0, 10.0)
    return out or None


# ---------------------------------------------------------------------------
# Rendering — axis values -> natural language. One shared table-driven
# helper, two phrasing tables (one for the model, one for the human).
# ---------------------------------------------------------------------------

AXIS_ADAPTATION_PHRASING = {
    "directness": {
        "high": "give it to them straight and skip the cushioning — they'd rather hear the blunt version",
        "low": "ease things in rather than dropping blunt takes on them cold — they land better with a gentler approach",
    },
    "banter": {
        "high": "lean into banter and teasing with them, they enjoy getting roasted a little",
        "low": "keep it more sincere with them and dial back the sarcasm/teasing",
    },
    "energy": {
        "high": "match their higher energy — more enthusiasm, less flat",
        "low": "keep your energy chill and low-key with them, don't over-hype replies",
    },
    "depth": {
        "high": "they genuinely enjoy longer, more thoughtful exchanges — it's fine to go deeper with them, not just quick banter",
        "low": "they tend to prefer quick, light exchanges — don't over-elaborate by default",
    },
    "support_style": {
        "high": "when something's wrong, they want practical help — offer something concrete, not just sympathy",
        "low": "when they're venting, they want to feel heard first — don't rush to fix it or advise unless asked",
    },
}

AXIS_USER_PHRASING = {
    "directness": {"high": "likes it straight-up, less cushioning", "low": "prefers things eased in gently"},
    "banter": {"high": "down for banter and teasing", "low": "prefers sincerity over sarcasm"},
    "energy": {"high": "matches high energy", "low": "prefers a chill, low-key tone"},
    "depth": {"high": "enjoys going deep on topics", "low": "prefers quick, light exchanges"},
    "support_style": {"high": "wants solutions when venting", "low": "wants to be heard first when venting"},
}


def _axis_bits(profile: dict | None, confidence: dict | None, phrasing_table: dict,
               min_confidence: float) -> list[str]:
    bits = []
    profile = profile or {}
    confidence = confidence or {}
    for axis, phrasing in phrasing_table.items():
        conf = confidence.get(axis, 0.0) or 0.0
        if conf < min_confidence:
            continue
        val = profile.get(axis, DEFAULT_AXIS_VALUE)
        if val >= 65:
            bits.append(phrasing["high"])
        elif val <= 35:
            bits.append(phrasing["low"])
    return bits


def render_adaptation_layer(profile: dict | None, confidence: dict | None,
                             *, min_confidence: float = MIN_CONFIDENCE_TO_RENDER) -> str | None:
    """System-prompt addendum, written as a standing instruction to the
    model. Returns None when there isn't enough signal on ANY axis yet —
    callers should skip appending anything in that case rather than
    injecting an empty/generic note."""
    bits = _axis_bits(profile, confidence, AXIS_ADAPTATION_PHRASING, min_confidence)
    if not bits:
        return None
    header = (
        "You've picked up a read on how this specific person likes to be talked to (this "
        "shapes delivery only — it never overrides your core personality or boundaries): "
    )
    return header + "; ".join(bits) + ". Weave this in naturally — never announce or explain that you're adapting."


def describe_style_for_user(profile: dict | None, confidence: dict | None,
                             *, min_confidence: float = MIN_CONFIDENCE_TO_RENDER) -> str:
    """Second-person, /myprofile-facing summary — same signal as
    render_adaptation_layer, phrased for the person themselves instead of
    as an instruction to the model. Always returns a string (never None) —
    /myprofile has a field to fill either way."""
    bits = _axis_bits(profile, confidence, AXIS_USER_PHRASING, min_confidence)
    if not bits:
        return "Still getting a read on your vibe — the more we talk (or run `/vibecheck`), the better I'll match it."
    return "So far: " + ", ".join(bits) + "."


# ---------------------------------------------------------------------------
# /vibecheck — 4 forced-choice questions, tap-only, ~15 seconds. One axis
# per question, big deltas since these are direct, trusted answers.
# Order chosen to open on the lowest-stakes question (directness) and close
# on the quickest/most fun one (energy).
# ---------------------------------------------------------------------------

VIBECHECK_QUESTIONS = [
    {
        "prompt": "If I think you're about to mess something up, I should:",
        "options": [
            {"label": "Tell me straight up", "delta": {"directness": 35}},
            {"label": "Ease into it a bit", "delta": {"directness": -35}},
        ],
    },
    {
        "prompt": "Quick banter check:",
        "options": [
            {"label": "Roast me, I can take it", "delta": {"banter": 35}},
            {"label": "Keep it sincere with me", "delta": {"banter": -35}},
        ],
    },
    {
        "prompt": "When I'm venting about something:",
        "options": [
            {"label": "Just listen, don't fix it", "delta": {"support_style": -35}},
            {"label": "Help me actually solve it", "delta": {"support_style": 35}},
        ],
    },
    {
        "prompt": "Ideal chat energy for you:",
        "options": [
            {"label": "Chill and low-key", "delta": {"energy": -30}},
            {"label": "Hyped and enthusiastic", "delta": {"energy": 30}},
        ],
    },
]