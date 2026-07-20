"""
utils/aysa_knowledge.py

Aysa's book/PDF knowledge library — admin-fed source material (books,
papers, PDFs) chunked and embedded into Postgres/pgvector, retrieved as
grounding context during chat instead of relying purely on the model's
trained knowledge.

Everything here is a no-op-safe wrapper around db.KNOWLEDGE_LIBRARY_AVAILABLE
— see utils/database.py's AYSA_VECTOR_SCHEMA comment for why that flag can
be False (no pgvector on the host) and how gracefully that degrades.
"""

from __future__ import annotations

import io
import re
import logging

from utils import database as db
from utils import gemini_client

logger = logging.getLogger("lucy.aysa_knowledge")

CHUNK_TARGET_CHARS = 1800   # ~450 tokens — comfortably inside embedding + prompt budgets
CHUNK_OVERLAP_CHARS = 200   # only used when hard-splitting an oversized paragraph


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


async def ingest_text(title: str, text: str, added_by: int) -> dict:
    """Chunks `text`, embeds each chunk, and stores it under a new
    knowledge source. Returns {"source_id", "title", "chunk_count",
    "failed_chunks"} — partial success is reported rather than hidden, so
    an admin running /aysaaddbook can see if e.g. a rate limit cut it
    short partway through a long book.

    Raises RuntimeError if the knowledge library isn't available at all
    (no pgvector) — callers should check db.KNOWLEDGE_LIBRARY_AVAILABLE
    first and show a clear message rather than let this raise.
    """
    if not db.KNOWLEDGE_LIBRARY_AVAILABLE:
        raise RuntimeError("Knowledge library is not available on this deployment (pgvector not installed).")

    chunks = _chunk_text(text)
    if not chunks:
        raise RuntimeError("No extractable text found to ingest.")

    source = await db.add_knowledge_source(title, added_by)
    stored = 0
    failed = 0
    for i, chunk in enumerate(chunks):
        try:
            embedding = await gemini_client.embed_text(chunk)
            await db.add_knowledge_chunk(source["id"], i, chunk, _vector_literal(embedding))
            stored += 1
        except Exception:
            logger.exception("Failed to embed/store chunk %d of '%s'", i, title)
            failed += 1

    return {"source_id": source["id"], "title": title, "chunk_count": stored, "failed_chunks": failed}


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


def extract_pdf_text(file_bytes: bytes) -> str:
    """Extracts plain text from a PDF's raw bytes using pypdf. Raises
    RuntimeError with a clear, user-facing message on failure (encrypted/
    scanned/corrupt PDF) rather than a raw pypdf traceback — the caller (a
    Discord command) surfaces this directly to the admin who ran it."""
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
        pages = [page.extract_text() or "" for page in reader.pages]
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Couldn't read this PDF: {e}") from e

    text = "\n\n".join(p for p in pages if p.strip())
    if not text.strip():
        raise RuntimeError(
            "No extractable text found — this PDF may be a scanned image without OCR text."
        )
    return text
