"""
utils/aysa_knowledge.py

Aysa's book/PDF knowledge library — admin-fed source material (books,
papers, PDFs) chunked and embedded into Postgres/pgvector, retrieved as
grounding context during chat instead of relying purely on the model's
trained knowledge.

Everything here is a no-op-safe wrapper around db.KNOWLEDGE_LIBRARY_AVAILABLE
— see utils/database.py's AYSA_VECTOR_SCHEMA comment for why that flag can
be False (no pgvector on the host) and how gracefully that degrades.

PDF extraction is two-tier: pypdf reads a real text layer directly (fast,
free, no API call); any page that comes back empty/near-empty — the case
for a PDF built from photographed pages, which has no text layer at all —
falls back to rendering that one page with pymupdf and OCR'ing it via
utils/openrouter_client.ocr_page_text (the same free vision router already
used for avatar descriptions elsewhere in this project, zero new API key).
Mixed-mode by page, not by document: a normal text PDF pays the OCR cost on
zero pages, a fully-scanned book pays it on every page.
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import time
import logging
from typing import Awaitable, Callable

import aiohttp

from utils import database as db
from utils import gemini_client
from utils import http
from utils import openrouter_client

logger = logging.getLogger("lucy.aysa_knowledge")

CHUNK_TARGET_CHARS = 1800   # ~450 tokens — see note below on why this is NOT being bumped up despite the volume problem
CHUNK_OVERLAP_CHARS = 200   # only used when hard-splitting an oversized paragraph

OCR_MIN_CHARS = 40          # pypdf text shorter than this on a page is treated as "no real text layer"
OCR_RENDER_DPI = 150        # legible for a vision model without producing huge base64 payloads
MAX_OCR_PAGES = 600         # sanity cap — a mis-sized upload can't run forever against a free-tier router

# Gemini's free-tier embedding quota turned out to be the real bottleneck, not code correctness —
# a first live run against OpenStax + 3 PD classics (~1800 chunks at this chunk size) took ~100
# minutes and still only got ~55% of chunks through, with later sources getting hit hardest (a
# rolling per-minute cap, not a hard daily wall — throughput held fairly steady around ~10/minute
# the whole time, so pacing calls to roughly match that, rather than firing them back-to-back and
# retrying after the fact, converts most of those failures into successes instead of just burning
# an hour on guaranteed 429s).
#
# CHUNK_TARGET_CHARS is deliberately NOT being made bigger to cut total request volume, even though
# that's the obvious lever — resumability below matches chunks by POSITIONAL INDEX against what's
# already stored (see ingest_text), and ~1002 chunks are already sitting in Postgres from tonight's
# run under this exact chunk size. Changing it would silently desync every index against that
# existing data on the very next run. If you want fewer/bigger chunks going forward, do it as its
# own deliberate step: clear the existing partial sources first (/aysaremovebook), THEN change this
# constant, THEN re-run — not both at once.
#
# Two changes address the actual bottleneck instead:
#   1. EMBED_MIN_CALL_INTERVAL_SECONDS proactively paces every call to roughly this rate BEFORE
#      hitting a 429, instead of firing as fast as possible and only backing off after failing.
#   2. ingest_text is now resumable by title — a second run (today, tomorrow, whenever) against a
#      partially-ingested source picks up exactly where it left off instead of starting over, and
#      stops itself early (RATE_LIMIT_CIRCUIT_BREAKER) after a run of consecutive rate-limited
#      chunks instead of grinding through the entire remaining list at a near-100% failure rate.
EMBED_MAX_RETRIES = 3
EMBED_RETRY_BASE_SECONDS = 8.0            # backoff after a 429 on one chunk: 8s, then 16s
EMBED_MIN_CALL_INTERVAL_SECONDS = 4.5     # ~13/minute — matched to what last night's run actually sustained
RATE_LIMIT_CIRCUIT_BREAKER = 6            # consecutive rate-limited chunks before giving up on the rest of THIS run

_last_embed_call_at = 0.0  # module-level pacing clock — good enough for a single-process bot


def _chunk_text(text: str) -> list[str]:
    """Simple paragraph-aware chunker: fills each chunk up to
    CHUNK_TARGET_CHARS, splitting on paragraph breaks where possible so a
    chunk doesn't cut a paragraph in half more often than necessary. Good
    enough for book/paper text; not trying to be a sentence-boundary-perfect
    tokenizer."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= CHUNK_TARGET_CHARS:
            current = f"{current}\n\n{para}" if current else para
            continue
        if current:
            chunks.append(current)
        if len(para) <= CHUNK_TARGET_CHARS:
            current = para
        else:
            # A single paragraph longer than the target — hard-split it.
            step = CHUNK_TARGET_CHARS - CHUNK_OVERLAP_CHARS
            for i in range(0, len(para), step):
                chunks.append(para[i:i + CHUNK_TARGET_CHARS])
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _vector_literal(values: list[float]) -> str:
    """Formats a float list as a pgvector text literal — see
    db.add_knowledge_chunk for why this is a plain string, not a codec."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


async def _embed_with_retry(chunk: str) -> list[float]:
    """Gemini embeddings are single-key/single-project — no round-robin
    fallback like Groq/Cerebras/OpenRouter (see gemini_client's module
    docstring) — so a book-length ingest is exactly the kind of bursty
    load that trips its rate limit. Two layers here, not one:

    1. Proactive pacing (EMBED_MIN_CALL_INTERVAL_SECONDS) — wait out the
       minimum interval since the LAST call, win or lose, before every
       attempt. A live run showed throughput holding fairly steady around
       ~10/minute for 100 minutes rather than a hard day-long wall, which
       means most of the failures were us firing faster than the limit
       recovers, not the limit being exhausted for the day. Pacing to
       roughly match it converts a lot of those into successes instead of
       429s, rather than just retrying failures after the fact.
    2. Reactive backoff on an actual 429 (EMBED_RETRY_BASE_SECONDS) — for
       whatever pacing alone doesn't catch.

    Only retries what looks like a rate limit; a hard error (bad key,
    malformed request, deprecated model) fails immediately since retrying
    wouldn't change the outcome."""
    global _last_embed_call_at
    last_error: Exception | None = None
    for attempt in range(EMBED_MAX_RETRIES):
        elapsed = time.monotonic() - _last_embed_call_at
        if elapsed < EMBED_MIN_CALL_INTERVAL_SECONDS:
            await asyncio.sleep(EMBED_MIN_CALL_INTERVAL_SECONDS - elapsed)
        _last_embed_call_at = time.monotonic()
        try:
            return await gemini_client.embed_text(chunk)
        except Exception as e:
            last_error = e
            if "rate-limited" not in str(e).lower() and "429" not in str(e):
                raise
            if attempt < EMBED_MAX_RETRIES - 1:
                await asyncio.sleep(EMBED_RETRY_BASE_SECONDS * (2 ** attempt))
    raise last_error


async def ingest_text(
    title: str, text: str, added_by: int,
    *, progress_cb: "Callable[[int, int], Awaitable[None]] | None" = None,
) -> dict:
    """Chunks `text`, embeds each chunk, and stores it under a knowledge
    source. Returns {"source_id", "title", "chunk_count", "failed_chunks",
    "first_error", "aborted_early"}. first_error carries the actual
    exception text from the first failure (e.g. a deprecated/renamed
    model, a bad API key, a rate limit) so an admin can see WHY straight
    from Discord instead of needing to check server logs.

    Resumable by title: if a source with this exact title already exists
    (e.g. a prior run got partway through before the rate limit forced it
    to stop), this continues it — chunk indices already embedded are
    skipped, not redone — rather than creating a duplicate source and
    burning quota re-embedding chunks that already succeeded. Chunking is
    deterministic given the same text and CHUNK_TARGET_CHARS/
    CHUNK_OVERLAP_CHARS, so the same input reliably produces the same
    chunk-index-to-content mapping across runs.

    aborted_early is True if RATE_LIMIT_CIRCUIT_BREAKER consecutive chunks
    got rate-limited — at that point continuing through the rest of the
    list would almost certainly just be more of the same, so this stops
    and reports how far it got rather than grinding for an hour at a
    near-100% failure rate. Just re-run it later (same title) to pick up
    the remainder.

    progress_cb(chunks_done, chunks_total), if given, is awaited after
    every chunk attempted (including skipped/already-done ones) — used by
    /aysaaddbook and /aysaseedlibrary to post progress on a job that can
    run long.

    Raises RuntimeError if the knowledge library isn't available at all
    (no pgvector) — callers should check db.KNOWLEDGE_LIBRARY_AVAILABLE
    first and show a clear message rather than let this raise.
    """
    if not db.KNOWLEDGE_LIBRARY_AVAILABLE:
        raise RuntimeError("Knowledge library is not available on this deployment (pgvector not installed).")

    chunks = _chunk_text(text)
    if not chunks:
        raise RuntimeError("No extractable text found to ingest.")

    existing_source = await db.find_knowledge_source_by_title(title)
    if existing_source is not None:
        source = existing_source
        already_done = await db.get_embedded_chunk_indices(source["id"])
        logger.info("Resuming ingest of '%s' — %d/%d chunk(s) already embedded.", title, len(already_done), len(chunks))
    else:
        source = await db.add_knowledge_source(title, added_by)
        already_done = set()

    stored = len(already_done)
    failed = 0
    first_error: str | None = None
    consecutive_rate_limited = 0
    aborted_early = False

    for i, chunk in enumerate(chunks):
        if i not in already_done:
            try:
                embedding = await _embed_with_retry(chunk)
                await db.add_knowledge_chunk(source["id"], i, chunk, _vector_literal(embedding))
                stored += 1
                consecutive_rate_limited = 0
            except Exception as e:
                logger.exception("Failed to embed/store chunk %d of '%s'", i, title)
                failed += 1
                if first_error is None:
                    first_error = str(e)
                if "rate-limited" in str(e).lower() or "429" in str(e):
                    consecutive_rate_limited += 1
                    if consecutive_rate_limited >= RATE_LIMIT_CIRCUIT_BREAKER:
                        logger.warning(
                            "'%s' rate-limited %d times in a row — stopping at chunk %d/%d instead of "
                            "grinding through the rest at a near-certain failure rate. Re-run later to resume.",
                            title, consecutive_rate_limited, i + 1, len(chunks),
                        )
                        aborted_early = True
                        failed += len(chunks) - i - 1  # count the untried remainder honestly, not as silent success
                        break
                else:
                    consecutive_rate_limited = 0
        if progress_cb is not None:
            try:
                await progress_cb(i + 1, len(chunks))
            except Exception:
                logger.exception("progress_cb raised during embedding — continuing ingest anyway.")

    return {
        "source_id": source["id"], "title": title, "chunk_count": stored,
        "failed_chunks": failed, "first_error": first_error, "aborted_early": aborted_early,
    }


async def search_knowledge(query: str, top_k: int = 4) -> list[dict]:
    """Returns up to top_k {content, source_title, distance} matches for
    `query`, or [] if the library is unavailable/empty/errors — this feeds
    a tool result handed back to the model, so it degrades to 'nothing
    found' rather than raising into the chat pipeline (same philosophy as
    utils/facts.py)."""
    if not db.KNOWLEDGE_LIBRARY_AVAILABLE:
        return []
    try:
        embedding = await gemini_client.embed_text(query)
        return await db.search_knowledge_chunks(_vector_literal(embedding), top_k=top_k)
    except Exception:
        logger.exception("Knowledge search failed for query: %r", query)
        return []


async def extract_pdf_text_with_ocr(
    file_bytes: bytes,
    *, progress_cb: "Callable[[int, int, int], Awaitable[None]] | None" = None,
) -> tuple[str, dict]:
    """Extracts text page-by-page: pypdf first (free, instant), and for any
    page whose text comes back under OCR_MIN_CHARS — the signature of a
    page with no real text layer, i.e. a photographed page — renders just
    that page with pymupdf and OCR's it via openrouter_client.ocr_page_text.

    A single page's OCR failing does NOT abort the whole book; it's
    recorded in stats["ocr_failures"] and skipped, same partial-success
    philosophy as ingest_text below.

    progress_cb(pages_done, pages_total, pages_ocrd), if given, is awaited
    after every page — /aysaaddbook uses this to post progress, since OCR'ing
    a few hundred pages can run well past Discord's ~15-minute interaction
    window.

    Returns (text, stats) where stats has pages_total/pages_ocrd/
    pages_empty/ocr_failures. Raises RuntimeError only for things no
    per-page fallback can fix: pypdf missing, an undecryptable encrypted
    file, zero pages, or — after trying every page — zero extractable text
    at all (e.g. every page is scanned AND no OCR provider is configured).
    """
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError("pypdf is not installed — add it to requirements.txt.") from e

    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as e:
                raise RuntimeError("This PDF is password-protected — remove the password and try again.") from e
        page_count = len(reader.pages)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Couldn't read this PDF: {e}") from e

    if page_count == 0:
        raise RuntimeError("This PDF has no pages.")

    pages_to_process = min(page_count, MAX_OCR_PAGES)
    if page_count > MAX_OCR_PAGES:
        logger.warning(
            "PDF has %d pages, above MAX_OCR_PAGES (%d) — only the first %d will be processed.",
            page_count, MAX_OCR_PAGES, pages_to_process,
        )

    ocr_available = openrouter_client.is_configured()
    fitz_doc = None  # opened lazily, only if a page actually needs OCR

    page_texts: list[str] = []
    pages_ocrd = 0
    pages_empty = 0
    ocr_failures = 0

    for i in range(pages_to_process):
        try:
            page_text = (reader.pages[i].extract_text() or "").strip()
        except Exception:
            logger.warning("pypdf failed to extract text on page %d — will try OCR.", i + 1)
            page_text = ""

        if len(page_text) < OCR_MIN_CHARS:
            if not ocr_available:
                pages_empty += 1
            else:
                if fitz_doc is None:
                    try:
                        import fitz  # pymupdf
                        fitz_doc = fitz.open(stream=file_bytes, filetype="pdf")
                    except Exception:
                        logger.exception("Failed to open PDF with pymupdf for OCR rendering — is pymupdf installed?")
                        fitz_doc = False  # sentinel: don't retry opening on every remaining page
                if fitz_doc:
                    try:
                        pix = fitz_doc[i].get_pixmap(dpi=OCR_RENDER_DPI)
                        data_uri = f"data:image/png;base64,{base64.b64encode(pix.tobytes('png')).decode('ascii')}"
                        ocr_text = await openrouter_client.ocr_page_text(data_uri)
                        if ocr_text:
                            page_text = ocr_text
                            pages_ocrd += 1
                        else:
                            pages_empty += 1
                    except Exception:
                        logger.exception("OCR failed on page %d", i + 1)
                        ocr_failures += 1
                else:
                    pages_empty += 1

        if page_text:
            page_texts.append(page_text)

        if progress_cb is not None:
            try:
                await progress_cb(i + 1, pages_to_process, pages_ocrd)
            except Exception:
                logger.exception("progress_cb raised during PDF extraction — continuing anyway.")

    text = "\n\n".join(page_texts)
    stats = {
        "pages_total": pages_to_process,
        "pages_ocrd": pages_ocrd,
        "pages_empty": pages_empty,
        "ocr_failures": ocr_failures,
    }
    if not text.strip():
        reason = (
            "no OCR provider is configured (set OPENROUTER_API_KEY)" if not ocr_available
            else "OCR couldn't read any of them either"
        )
        raise RuntimeError(f"No extractable text found — every page looks scanned and {reason}.")
    return text, stats


# ---------------------------------------------------------------------------
# Starter library — a hand-picked, copyright-safe set of foundational
# psychology sources fed to /aysaseedlibrary in one pass. Every entry is
# public domain or openly-licensed and was confirmed reachable without a
# login at the time this was written — deliberately a short, high-quality
# list rather than a big one, and deliberately NOT modern copyrighted
# self-help books: those get stored verbatim and served back in chunks, so
# only PD/openly-licensed material belongs here. See build_system_prompt's
# knowledge-library guidance in cogs/aysa_chat.py for how Aysa is expected
# to actually use this material conversationally, not lecture from it.
# ---------------------------------------------------------------------------

STARTER_LIBRARY_SOURCES = [
    {
        "title": "Psychology 2e (OpenStax)",
        "url": "https://archive.org/download/cnx-org-col31502/psychology-2e.pdf",
        "kind": "pdf",
    },
    {
        "title": "Psychology: Briefer Course (William James, 1892)",
        "url": "https://www.gutenberg.org/cache/epub/55262/pg55262.txt",
        "kind": "txt",
    },
    {
        "title": "The Enchiridion (Epictetus, tr. Higginson)",
        "url": "https://www.gutenberg.org/cache/epub/45109/pg45109.txt",
        "kind": "txt",
    },
    {
        "title": "Meditations (Marcus Aurelius, tr. Long)",
        "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
        "kind": "txt",
    },
]


async def _fetch_url_bytes(url: str, timeout_seconds: int = 90) -> bytes:
    session = await http.get_session()
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with session.get(url, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Fetching {url} returned HTTP {resp.status}")
        return await resp.read()


async def seed_starter_library(
    added_by: int,
    *, progress_cb: "Callable[[str, int, int], Awaitable[None]] | None" = None,
) -> list[dict]:
    """Fetches and ingests STARTER_LIBRARY_SOURCES one at a time. A single
    source failing (dead link, network hiccup) is recorded and skipped
    rather than aborting the rest — same partial-success philosophy as
    ingest_text. progress_cb(source_title, index, total) is awaited before
    each source starts, so a caller can post "fetching 2/4: ..." to Discord.

    If one source's ingest_text comes back aborted_early (sustained rate
    limiting — see that function), remaining sources are skipped rather
    than each independently re-discovering the same limit: a fresh source
    still needs RATE_LIMIT_CIRCUIT_BREAKER consecutive failures before ITS
    circuit breaker trips, which would otherwise cost a few wasted minutes
    per remaining source confirming what we already know this run. Just
    re-run this command later — every source (including the skipped ones)
    resumes via ingest_text's title-based matching rather than starting over.

    Returns a list of per-source result dicts: ingest_text's normal shape
    on success, or {"title", "error"} for one that failed outright or was
    skipped.
    """
    if not db.KNOWLEDGE_LIBRARY_AVAILABLE:
        raise RuntimeError("Knowledge library is not available on this deployment (pgvector not installed).")

    results = []
    rate_limited_stop = False
    for idx, source in enumerate(STARTER_LIBRARY_SOURCES, start=1):
        if rate_limited_stop:
            results.append({
                "title": source["title"],
                "error": "Skipped — an earlier source hit a sustained rate limit this run. Re-run this command later to pick up here.",
            })
            continue
        if progress_cb is not None:
            try:
                await progress_cb(source["title"], idx, len(STARTER_LIBRARY_SOURCES))
            except Exception:
                logger.exception("progress_cb raised during seed_starter_library — continuing anyway.")
        try:
            raw = await _fetch_url_bytes(source["url"])
            if source["kind"] == "pdf":
                text, _stats = await extract_pdf_text_with_ocr(raw)
            else:
                text = raw.decode("utf-8", errors="replace")
            result = await ingest_text(source["title"], text, added_by)
            results.append(result)
            if result.get("aborted_early"):
                rate_limited_stop = True
        except Exception as e:
            logger.exception("Failed to seed starter source '%s'", source["title"])
            results.append({"title": source["title"], "error": str(e)})
    return results