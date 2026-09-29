"""C4 — turn normalised units into chunks with full provenance.

The strategy and every constant used here are justified in
doc/chunking-strategy.md, which was written before this module.

Summary: one fact per chunk, packed from the units produced by
`src.loader`, target 320 chars, hard cap 700, and no overlap — except when a
single unit is too long to be one chunk, where sentences are packed and the
boundary sentence is repeated so no sentence is lost.

The measured reason for this shape: the median fact on these pages is 13
characters, so a prose-sized chunk would mix 30-70 unrelated facts and could not
produce the short, single-fact answers the architecture requires.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

from src import config
from src.loader import LoadedDocument, _drop_sentences as loader_drop_sentences
from src.loader import _sentence_split

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


@dataclass(frozen=True)
class Chunk:
    """One embeddable, citable unit of text.

    `text` is what gets embedded and shown to the answering model, and already
    carries the scheme and section prefix. `body` is the fact on its own, kept
    for verification and for readable output.
    """

    chunk_id: str
    source_url: str
    source_title: str
    scheme_name: str
    doc_type: str
    section: str
    chunk_index: int
    fetched_at: str
    content_hash: str
    text: str
    body: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def prefixed_text(scheme_name: str, section: str, body: str) -> str:
    """Make a fact self-describing before it is embedded.

    A bare "1.21%" is meaningless to a 384-dimension encoder. The scheme name
    and section are prepended so the embedding, and the model reading it, both
    know which scheme and which field the fact belongs to.
    """
    return f"{scheme_name} \u2014 {section}: {body}"


def _pack(sentences: list[str], limit: int, overlap: int) -> list[str]:
    """Greedily pack sentences into groups of at most `limit` characters.

    `overlap` sentences from the end of one group are repeated at the start of
    the next, so a fact that straddles a boundary is never cut in half.
    """
    groups: list[str] = []
    current: list[str] = []
    length = 0

    for sentence in sentences:
        if len(sentence) > limit:
            # A single sentence longer than the cap: flush, then hard-wrap it.
            if current:
                groups.append(" ".join(current))
                current, length = [], 0
            for start in range(0, len(sentence), limit):
                groups.append(sentence[start:start + limit])
            continue
        if current and length + len(sentence) + 1 > limit:
            groups.append(" ".join(current))
            carry = current[-overlap:] if overlap else []
            current = list(carry)
            length = sum(len(s) for s in current) + max(0, len(current) - 1)
        current.append(sentence)
        length += len(sentence) + 1

    if current:
        groups.append(" ".join(current))
    return [g for g in groups if g.strip()]


def split_body(body: str) -> list[str]:
    """Split one unit into sentence-aligned pieces, each within the cap.

    A unit that already fits is returned unchanged, which is the common case:
    755 of 755 measured units are at most 174 characters.
    """
    if len(body) <= config.CHUNK_TARGET_CHARS:
        return [body]

    sentences = _sentence_split(body)
    if len(sentences) <= 1:
        # No sentence boundary to align to; fall back to a hard wrap at the cap.
        return [
            body[i:i + config.CHUNK_MAX_CHARS]
            for i in range(0, len(body), config.CHUNK_MAX_CHARS)
        ]

    return _pack(sentences, config.CHUNK_TARGET_CHARS, config.CHUNK_OVERLAP_SENTENCES)


def build_chunks(document: LoadedDocument) -> list[Chunk]:
    """Build every chunk for one loaded document, in page order."""
    source = document.source
    chunks: list[Chunk] = []
    index = 0

    for unit in document.units:
        body = " ".join(loader_drop_sentences(unit.text).split())
        if len(body) < config.MIN_UNIT_CHARS:
            continue
        for piece in split_body(body):
            piece = piece.strip()
            if not piece:
                continue
            chunks.append(
                Chunk(
                    chunk_id=f"{source.slug}-{index:03d}",
                    source_url=source.source_url,
                    source_title=source.page_name,
                    scheme_name=source.page_name,
                    doc_type=source.doc_type,
                    section=unit.section,
                    chunk_index=index,
                    fetched_at=document.fetched_at,
                    content_hash=document.content_hash,
                    text=prefixed_text(source.page_name, unit.section, piece),
                    body=piece,
                )
            )
            index += 1

    return chunks


def build_corpus_chunks(documents: list[LoadedDocument]) -> list[Chunk]:
    """Build chunks for every document, preserving registry order."""
    chunks: list[Chunk] = []
    for document in documents:
        chunks.extend(build_chunks(document))
    return chunks


def corpus_hash(chunks: list[Chunk]) -> str:
    """Stable hash of the whole corpus, for the ingestion manifest."""
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk.chunk_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(chunk.text.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


__all__ = [
    "Chunk",
    "build_chunks",
    "build_corpus_chunks",
    "corpus_hash",
    "prefixed_text",
    "split_body",
]
