"""
utils/aysa_content.py

Content sourcing for Aysa's course lessons — finds a real, existing
YouTube video and academic paper on a topic (rather than inventing
sources), then has the AI model write the actual lecture material
(summary + comprehension questions) Aysa teaches from.

Two free/no-surprise-key sources, same "defensive, never raises into the
caller" style as utils/facts.py — a failed lookup degrades to "couldn't
find one," never a crash that blocks lesson creation:
  - YouTube Data API v3 search — needs YOUTUBE_API_KEY (free tier: 10k
    units/day, a search.list call costs 100 units, so roughly 100
    searches/day). Optional — if unset, video sourcing is just skipped and
    lessons ship paper-only.
  - Semantic Scholar Graph API — free, no key required. An optional
    SEMANTIC_SCHOLAR_API_KEY raises the shared rate limit if you have one.
"""

from __future__ import annotations

import os
import json
import logging

import aiohttp

from utils import http
from utils import nim_client
from utils import groq_client

logger = logging.getLogger("lucy.aysa_content")

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
SEMANTIC_SCHOLAR_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
REQUEST_TIMEOUT_SECONDS = 10


def _youtube_key() -> str:
    return os.getenv("YOUTUBE_API_KEY", "").strip()


async def search_youtube_video(topic: str) -> dict | None:
    """Returns {title, url, channel, description} for the best-matching
    video, or None if unavailable/unconfigured/not found. Never raises."""
    api_key = _youtube_key()
    if not api_key:
        logger.info("YOUTUBE_API_KEY not set — skipping video search for '%s'.", topic)
        return None

    params = {
        "part": "snippet",
        "q": f"{topic} psychology",
        "type": "video",
        "maxResults": 5,
        "relevanceLanguage": "en",
        "safeSearch": "strict",
        "videoEmbeddable": "true",
        "key": api_key,
    }
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    try:
        session = await http.get_session()
        async with session.get(YOUTUBE_SEARCH_URL, params=params, timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning("YouTube search failed (%s) for '%s'", resp.status, topic)
                return None
            data = await resp.json()
    except Exception:
        logger.exception("YouTube search request failed for '%s'", topic)
        return None

    items = data.get("items") or []
    if not items:
        return None
    top = items[0]
    video_id = top.get("id", {}).get("videoId")
    if not video_id:
        return None
    snippet = top.get("snippet", {})
    return {
        "title": snippet.get("title", "").strip(),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "channel": snippet.get("channelTitle", "").strip(),
        "description": (snippet.get("description") or "").strip(),
    }


async def search_paper(topic: str) -> dict | None:
    """Returns {title, url, authors, abstract, year} for the best-matching,
    reasonably-cited paper, or None. Never raises."""
    params = {
        "query": f"{topic} psychology",
        "fields": "title,abstract,authors,year,url,citationCount,openAccessPdf",
        "limit": 5,
    }
    headers = {}
    s2_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    if s2_key:
        headers["x-api-key"] = s2_key

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    try:
        session = await http.get_session()
        async with session.get(
            SEMANTIC_SCHOLAR_SEARCH_URL, params=params, headers=headers, timeout=timeout
        ) as resp:
            if resp.status != 200:
                logger.warning("Semantic Scholar search failed (%s) for '%s'", resp.status, topic)
                return None
            data = await resp.json()
    except Exception:
        logger.exception("Semantic Scholar search request failed for '%s'", topic)
        return None

    papers = [p for p in (data.get("data") or []) if p.get("abstract")]
    if not papers:
        return None
    # Prefer the most-cited result that actually has an abstract, rather
    # than just the first hit — a better proxy for "credible" than raw
    # search relevance alone.
    papers.sort(key=lambda p: p.get("citationCount") or 0, reverse=True)
    top = papers[0]
    authors = ", ".join(a.get("name", "") for a in (top.get("authors") or [])[:4])
    open_pdf = (top.get("openAccessPdf") or {}).get("url")
    return {
        "title": (top.get("title") or "").strip(),
        "url": open_pdf or top.get("url") or "",
        "authors": authors,
        "abstract": (top.get("abstract") or "").strip(),
        "year": top.get("year"),
    }


async def build_lesson_material(topic: str, video: dict | None, paper: dict | None) -> dict:
    """Has the AI model write the actual lecture content — a plain-language
    summary Aysa delivers, plus 3 comprehension/reflection questions she
    asks once the student says they've watched/read the source(s).
    Grounded in the real title/description/abstract fetched above, not
    invented from the topic name alone.

    Returns {"summary": str, "questions": list[str]}. Falls back to a
    source-free summary (less specific, still usable) if generation fails
    outright — this should never block a lesson from being created.
    """
    sources = []
    if video:
        sources.append(f"Video: \"{video['title']}\" by {video['channel']}\n{video['description'][:600]}")
    if paper:
        sources.append(
            f"Paper: \"{paper['title']}\" ({paper.get('year') or 'n.d.'}), {paper['authors']}\n"
            f"Abstract: {paper['abstract'][:800]}"
        )
    source_block = "\n\n".join(sources) if sources else "(No specific source found — write from general knowledge.)"

    system = (
        "You are drafting one lesson for a structured psychology curriculum, in the voice of a "
        "warm, clear teacher. Given a topic and (usually) a real source video/paper, write: "
        "1) a plain-language lesson summary (250-400 words, no unexplained jargon, "
        "evidence-informed, grounded in the source's actual claims rather than invented ones), "
        "2) exactly 3 short comprehension/reflection questions a mentor would ask a student "
        "after they watched/read it — a mix of 'did the idea land' and 'how does this show up "
        "in your own life' questions. "
        "Output strict JSON only, no markdown fences, exactly this shape: "
        '{"summary": "...", "questions": ["...", "...", "..."]}'
    )
    user_content = f"Topic: {topic}\n\nSource material:\n{source_block}"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]

    raw = None
    try:
        raw = await nim_client.call_nim(messages, max_tokens=900, temperature=0.6)
    except Exception as e:
        logger.warning("build_lesson_material via NIM failed for '%s': %s", topic, e)
        if groq_client.is_configured():
            try:
                raw = await groq_client.call_groq(
                    messages, model=groq_client.MODEL_QUALITY, max_tokens=900, temperature=0.6
                )
            except Exception as e2:
                logger.warning("build_lesson_material via Groq also failed for '%s': %s", topic, e2)

    if raw:
        cleaned = raw.strip()
        for fence in ("```json", "```"):
            if cleaned.startswith(fence):
                cleaned = cleaned[len(fence):]
        if cleaned.endswith("```"):
            cleaned = cleaned[: -len("```")]
        cleaned = cleaned.strip()
        try:
            parsed = json.loads(cleaned)
            summary = (parsed.get("summary") or "").strip()
            questions = [q.strip() for q in (parsed.get("questions") or []) if q.strip()]
            if summary:
                return {"summary": summary, "questions": questions[:3]}
        except (json.JSONDecodeError, AttributeError):
            logger.warning("build_lesson_material returned non-JSON for '%s', using raw text as summary", topic)
            return {"summary": raw.strip(), "questions": []}

    # Total failure — still return something usable rather than blocking lesson creation.
    return {
        "summary": f"(Auto-summary unavailable right now — try /aysaaddlesson again later.) Topic: {topic}.",
        "questions": [],
    }


async def source_lesson(topic: str) -> dict:
    """One-stop call used by cogs/aysa_courses.py's /aysaaddlesson: finds a
    video + paper, writes the lecture material, and returns everything
    db.add_lesson() needs as keyword arguments."""
    video = await search_youtube_video(topic)
    paper = await search_paper(topic)
    material = await build_lesson_material(topic, video, paper)
    return {
        "video_url": video["url"] if video else None,
        "video_title": video["title"] if video else None,
        "paper_url": paper["url"] if paper else None,
        "paper_title": paper["title"] if paper else None,
        "summary": material["summary"],
        "comprehension_questions": json.dumps(material["questions"]),
    }
