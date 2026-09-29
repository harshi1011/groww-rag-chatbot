"""C12 and C13 — question embedding, top-k retrieval, and the relevance gate.

Query-time only. This module deliberately imports neither `loader` nor
`chunker`: the corpus already exists on disk, and architecture §9 makes the
ingestion/query boundary a review-checkable import boundary. It reads the store
through `src.vectorstore`'s read half and embeds the question through the same
`src.embedder.embed` used at ingestion time, which is what makes a question and
a chunk comparable (architecture §10.1).

The relevance gate is the part that matters. Without it, a question about
cryptocurrency would still retrieve *something* — these five pages all discuss
"investment" — and the model would answer from the nearest irrelevant chunk. The
gate turns that into an honest "not in my sources" (architecture §5.4).
"""

from __future__ import annotations

from dataclasses import dataclass

from src import config, embedder, guardrails, sources, vectorstore


@dataclass(frozen=True)
class RetrievedChunk:
    """One retrieved chunk with its metadata and its cosine distance.

    Metadata travels with the text (architecture §4.2), so the mapping from
    `chunk_id` to `source_url` is always available without a second lookup.
    """

    chunk_id: str
    text: str
    distance: float
    source_url: str
    source_title: str
    section: str
    scheme_name: str
    doc_type: str
    chunk_index: int
    fetched_at: str
    content_hash: str

    @classmethod
    def from_result(cls, ids: list[str], documents: list, metadatas: list,
                    distances: list) -> "RetrievedChunk":
        return cls(
            chunk_id=ids,
            text=documents or "",
            distance=float(distances),
            source_url=metadatas.get("source_url", ""),
            source_title=metadatas.get("source_title", ""),
            section=metadatas.get("section", ""),
            scheme_name=metadatas.get("scheme_name", ""),
            doc_type=metadatas.get("doc_type", ""),
            chunk_index=int(metadatas.get("chunk_index", 0) or 0),
            fetched_at=metadatas.get("fetched_at", ""),
            content_hash=metadatas.get("content_hash", ""),
        )


@dataclass(frozen=True)
class RetrievalResult:
    """Top-k hits plus the relevance verdict for the whole set."""

    hits: tuple[RetrievedChunk, ...]
    relevant: bool
    best_distance: float | None
    threshold: float
    reason: str = ""

    def __bool__(self) -> bool:
        return self.relevant

    def by_id(self, chunk_id: str) -> RetrievedChunk | None:
        for hit in self.hits:
            if hit.chunk_id == chunk_id:
                return hit
        return None


def search(question: str, k: int | None = None) -> tuple[RetrievedChunk, ...]:
    """Embed the question and return the k nearest chunks, best first.

    The question is passed through `sources.expand_aliases` before embedding,
    which rewrites a brief-name scheme ("HDFC Equity Fund") to the name the
    corpus actually uses ("HDFC Flexi Cap Direct Plan Growth"). This is a
    query-time rewrite of the *question only*: no chunk, embedding, or Chroma
    record is touched. Questions with no alias embed byte-identical text to
    before, so the calibrated threshold bands are unaffected.

    Split out from `retrieve` so the relevance gate can be exercised on a fixed
    score list without a store, and so a caller that wants raw hits (the Phase 5
    verification does, to inspect top-k by hand) can skip the gate.
    """
    if k is None:
        if config.RETRIEVAL_TOP_K is None:
            config.require_tuning()
        k = config.RETRIEVAL_TOP_K

    embedding = embedder.embed_one(sources.expand_aliases(question))
    result = vectorstore.query(embedding, top_k=k)
    hits = tuple(
        RetrievedChunk.from_result(
            ids=result["ids"][0][i],
            documents=(result["documents"][0] or [""])[i],
            metadatas=result["metadatas"][0][i],
            distances=result["distances"][0][i],
        )
        for i in range(len(result["ids"][0]))
    )
    # Chroma returns ascending cosine distance, but the gate's correctness
    # depends on the order, so it is established here rather than assumed.
    return tuple(sorted(hits, key=lambda hit: hit.distance))


def retrieve(
    question: str, k: int | None = None, threshold: float | None = None
) -> RetrievalResult:
    """Retrieve and gate in one step.

    `threshold` overrides the calibrated config value; the gate is only reachable
    with an explicit argument before Phase 5 calibration, which is deliberate —
    an uncalibrated threshold would silently decide what the bot claims not to
    know.
    """
    hits = search(question, k=k)
    scores = [hit.distance for hit in hits]
    gate = guardrails.check_relevance(scores, threshold=threshold)
    return RetrievalResult(
        hits=hits,
        relevant=gate.relevant,
        best_distance=gate.best_score,
        threshold=gate.threshold,
        reason=gate.reason,
    )


__all__ = ["RetrievalResult", "RetrievedChunk", "retrieve", "search"]
