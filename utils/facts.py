"""
utils/facts.py

Grounding tools for Lucy's "don't hallucinate specific facts" problem.
Both of these are genuinely free — no API key, no signup, no rate-limit
surprise waiting to bite us on Railway's free tier:

- get_weather(location):   Open-Meteo (geocoding + forecast). No key ever.
- search_fact(query):      Wikipedia's public REST API. No key ever.

Both are deliberately defensive in the same style as groq_client.py /
openrouter_client.py: network/parse failures return a short, honest
"couldn't find that" string rather than raising all the way up into the
chat pipeline — a failed lookup should degrade to "I couldn't check that
right now," never to a crash that drops the whole reply.
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp

logger = logging.getLogger("lucy.facts")

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WIKI_SEARCH_URL = "https://en.wikipedia.org/w/api.php"
WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"

REQUEST_TIMEOUT_SECONDS = 8

# https://open-meteo.com/en/docs — WMO weather interpretation codes, condensed
# to plain language. Grouped rather than exhaustive; good enough for "what's
# it doing outside" conversational use, not an aviation briefing.
_WEATHER_CODE_DESCRIPTIONS = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy with frost",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "rain showers", 82: "heavy rain showers",
    85: "light snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with light hail", 99: "thunderstorm with heavy hail",
}


def _describe_weather_code(code: Optional[int]) -> str:
    if code is None:
        return "conditions unclear"
    return _WEATHER_CODE_DESCRIPTIONS.get(int(code), "conditions unclear")


async def get_weather(location: str) -> str:
    """Look up current weather for a place name. Returns a short plain-English
    sentence, or a short honest failure message — never raises to the caller,
    since this runs as a tool result that gets handed straight back to the
    model to paraphrase."""
    location = (location or "").strip()
    if not location:
        return "No location was given, so I couldn't check the weather."

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                GEOCODE_URL, params={"name": location, "count": 1, "format": "json"}
            ) as resp:
                if resp.status != 200:
                    return f"Couldn't look up '{location}' right now (geocoding service returned {resp.status})."
                geo = await resp.json()

            results = geo.get("results") or []
            if not results:
                return f"Couldn't find a place called '{location}' to check the weather for."

            place = results[0]
            lat, lon = place.get("latitude"), place.get("longitude")
            resolved_name = place.get("name") or location
            admin = place.get("admin1")
            country = place.get("country")
            display_name = ", ".join(p for p in [resolved_name, admin, country] if p)

            if lat is None or lon is None:
                return f"Couldn't resolve coordinates for '{location}'."

            async with session.get(
                FORECAST_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                    "timezone": "auto",
                },
            ) as resp:
                if resp.status != 200:
                    return f"Couldn't fetch a forecast for {display_name} right now (status {resp.status})."
                data = await resp.json()

        current = data.get("current") or {}
        temp = current.get("temperature_2m")
        feels_like = current.get("apparent_temperature")
        humidity = current.get("relative_humidity_2m")
        wind = current.get("wind_speed_10m")
        condition = _describe_weather_code(current.get("weather_code"))

        if temp is None:
            return f"Got a response for {display_name} but it didn't include current conditions — try again shortly."

        parts = [f"In {display_name}, it's currently {condition}, {temp}°C"]
        if feels_like is not None:
            parts.append(f"(feels like {feels_like}°C)")
        line = " ".join(parts) + "."
        extras = []
        if humidity is not None:
            extras.append(f"{humidity}% humidity")
        if wind is not None:
            extras.append(f"wind at {wind} km/h")
        if extras:
            line += " " + ", ".join(extras).capitalize() + "."
        return line

    except Exception as e:
        logger.warning("get_weather failed for '%s': %s", location, e)
        return f"Couldn't check the weather for '{location}' right now — the weather service didn't respond in time."


async def search_fact(query: str) -> str:
    """Quick factual lookup for things like 'when did X happen', 'who is Y',
    'what is Z' — backed by Wikipedia's public API (no key required). Returns
    a short summary, or a short honest failure message. This is meant for
    grounding general-knowledge/date/identity questions, not for anything
    live/real-time (use get_weather for weather, the news digest for current
    headlines)."""
    query = (query or "").strip()
    if not query:
        return "No question was given to look up."

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                WIKI_SEARCH_URL,
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": 1,
                    "format": "json",
                },
                headers={"User-Agent": "LucyDiscordBot/1.0 (grounding tool)"},
            ) as resp:
                if resp.status != 200:
                    return f"Couldn't search for that right now (status {resp.status})."
                search_data = await resp.json()

            hits = ((search_data.get("query") or {}).get("search")) or []
            if not hits:
                return f"Couldn't find anything reliable on '{query}'."

            title = hits[0].get("title")
            if not title:
                return f"Couldn't find anything reliable on '{query}'."

            async with session.get(
                WIKI_SUMMARY_URL.format(title=title.replace(" ", "_")),
                headers={"User-Agent": "LucyDiscordBot/1.0 (grounding tool)"},
            ) as resp:
                if resp.status != 200:
                    return f"Found a matching topic ('{title}') but couldn't load details right now."
                summary_data = await resp.json()

        extract = (summary_data.get("extract") or "").strip()
        if not extract:
            return f"Found '{title}' but there's no summary available for it."
        return f"{title}: {extract}"

    except Exception as e:
        logger.warning("search_fact failed for '%s': %s", query, e)
        return f"Couldn't look that up right now — the fact-check service didn't respond in time."