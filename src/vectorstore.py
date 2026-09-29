"""C7 + C8 — persistent ChromaDB store, plus the ingestion manifest.

Write half (ingestion, offline):
    open a persistent client, create the collection with cosine distance, upsert
    chunks with vectors and the full metadata contract.

Read half (query time):
    open the existing collection and query top-k with metadata. The read half
    never writes and never ingests (architecture §9).

The collection metadata and the manifest both record the embedding model id, the
dimension, and the normalisation flag, so a later build can refuse to serve
against a store it did not create (architecture §10.3).

Cosine distance is fixed at collection creation and cannot be changed afterwards,
which is why `validate_store()` rejects a collection built with anything else.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone

from src import config
from src.config import ConfigError

#: The nine mandatory metadata fields from architecture §2.4, plus the body
#: text. `chunk_index` is an int; the rest are strings. Chroma accepts only
#: str/int/float/bool metadata values, so nothing here may be None.
METADATA_FIELDS: tuple[str, ...] = (
    "chunk_id",
    "source_url",
    "source_title",
    "scheme_name",
    "doc_type",
    "section",
    "chunk_index",
    "fetched_at",
    "content_hash",
)


#: ChromaDB ships a prebuilt Rust core for the vector index. On this machine
#: (Windows, CPython 3.12) that extension segfaults inside the native `upsert`
#: for every write, on every chromadb release from 0.6.3 to 1.5.9, at every
#: numpy version. The Python `SegmentAPI` backend does the same job through
#: `chroma-hnswlib` and does not crash, so the store is pinned to it. The knob is
#: chromadb's own documented switch and is applied with `setdefault`, so setting
#: CHROMA_API_IMPL yourself still wins.
CHROMA_API_IMPL_ENV = "CHROMA_API_IMPL"
CHROMA_API_IMPL_VALUE = "chromadb.api.segment.SegmentAPI"


def _import_chromadb():
    """Import chromadb lazily, so config and verify can be imported without it."""
    import os

    os.environ.setdefault(CHROMA_API_IMPL_ENV, CHROMA_API_IMPL_VALUE)

    import chromadb
    from chromadb.config import Settings

    return chromadb, Settings


def get_client():
    """Open the persistent ChromaDB client, creating the directory if needed."""
    chromadb, Settings = _import_chromadb()
    config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(config.CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False, allow_reset=False),
    )


def collection_metadata() -> dict[str, object]:
    """Provenance stored on the collection itself (architecture §10.3).

    `hnsw:space` is the only key Chroma interprets; the rest are provenance that
    `validate_store()` reads back and asserts on.
    """
    return {
        "hnsw:space": config.DISTANCE_METRIC,
        # Chroma defaults this to the CPU count, and this machine's hnswlib build
        # corrupts memory and segfaults inside add_items when it indexes with
        # more than one thread (it died partway through a 144-record write, and at
        # a different point on every run). One thread is deterministic and fast
        # enough at this corpus size.
        "hnsw:num_threads": 1,
        # Small graph parameters. Two reasons. Recall is not the constraint at
        # this size: 144 vectors is small enough that near-exhaustive search is
        # cheap, so the graph only has to be correct, not clever. And a leaner
        # graph allocates less per element, which is what keeps the index inside
        # this machine's hnswlib build's usable capacity.
        "hnsw:M": 8,
        "hnsw:construction_ef": 16,
        "hnsw:search_ef": 32,
        # Chroma keeps newly written vectors in a pure-numpy brute-force index
        # and only flushes them into the native hnswlib graph once a batch
        # reaches this size. This machine's hnswlib build segfaults the moment it
        # indexes more than ~96 vectors, which a 144-vector corpus exceeds.
        # Holding the flush above the corpus size keeps every record in the
        # numpy index, which Chroma queries alongside the graph and which is
        # exhaustive -- the more accurate of the two at a corpus this small. This
        # is the knob to revisit if the corpus ever grows past a few thousand
        # chunks and the native index becomes necessary.
        "hnsw:batch_size": 100000,
        "embedding_model_id": config.EMBEDDING_MODEL_ID,
        "embedding_dimension": config.EMBEDDING_DIMENSION,
        "normalize_embeddings": config.EMBEDDING_NORMALIZE,
        "embedding_device": config.EMBEDDING_DEVICE,
        "prompt_prefix": config.EMBEDDING_PROMPT_PREFIX,
        "schema_version": config.MANIFEST_SCHEMA_VERSION,
    }


def get_or_create_collection(client):
    """Return the collection, creating it with the pinned distance if absent."""
    return client.get_or_create_collection(
        name=config.COLLECTION_NAME,
        metadata=collection_metadata(),
    )


def open_collection(client=None):
    """Open the existing collection for reading. Raises if it does not exist."""
    client = client if client is not None else get_client()
    try:
        return client.get_collection(name=config.COLLECTION_NAME)
    except Exception as exc:  # chroma raises several unrelated types
        raise ConfigError(
            f"No collection {config.COLLECTION_NAME!r} in {config.CHROMA_DIR}. "
            f"Run ingestion first: "
            f".venv\\Scripts\\python.exe ingest.py"
        ) from exc


def drop_collection(client) -> None:
    """Delete the collection if it exists. Used by `--force` for a clean rebuild."""
    try:
        client.delete_collection(name=config.COLLECTION_NAME)
    except Exception:
        pass  # nothing to delete is the normal first-run case


def reset_store() -> None:
    """Clear the store for a clean rebuild.

    Deleting the collection alone can leave stale index files behind, so removing
    the directory is preferred. Windows refuses to unlink a file that another
    process has open, though, and verification deliberately holds a handle while
    it drives a rebuild. In that case fall back to dropping the collection
    through chromadb, which closes its own files; rebuilding writes every chunk
    under a stable id, so any surviving file is overwritten rather than
    duplicated.
    """
    if not config.CHROMA_DIR.exists():
        return
    try:
        shutil.rmtree(config.CHROMA_DIR)
        return
    except OSError as exc:
        print(
            f"    note: could not remove {config.CHROMA_DIR} ({exc}); "
            f"dropping the collection instead",
            flush=True,
        )
    drop_collection(get_client())


def _records(chunks) -> tuple[list[str], list[str], list[dict[str, object]]]:
    ids, documents, metadatas = [], [], []
    for chunk in chunks:
        missing = [f for f in METADATA_FIELDS if getattr(chunk, f) in (None, "")]
        if missing:
            raise ConfigError(
                f"Chunk {chunk.chunk_id} is missing metadata {missing}. "
                f"Refusing to write an incomplete record."
            )
        ids.append(chunk.chunk_id)
        documents.append(chunk.text)
        metadatas.append({f: getattr(chunk, f) for f in METADATA_FIELDS})
    return ids, documents, metadatas


def write_chunks(chunks, embeddings) -> int:
    """Upsert chunks and vectors in batches. Returns the number written.

    Raises before writing anything if the vector count does not match the chunk
    count, so a partial store cannot be produced by a bad embed call.
    """
    if len(embeddings) != len(chunks):
        raise ConfigError(
            f"Embedding count {len(embeddings)} does not match chunk count "
            f"{len(chunks)}. Aborting; no store was written."
        )

    ids, documents, metadatas = _records(chunks)
    client = get_client()
    collection = get_or_create_collection(client)

    written = 0
    batches = (len(ids) + config.STORE_BATCH_SIZE - 1) // config.STORE_BATCH_SIZE
    for number, start in enumerate(
        range(0, len(ids), config.STORE_BATCH_SIZE), start=1
    ):
        stop = min(start + config.STORE_BATCH_SIZE, len(ids))
        print(
            f"    batch {number}/{batches}: {stop - start} records"
            f" ({ids[start]} .. {ids[stop - 1]})",
            flush=True,
        )
        collection.upsert(
            ids=ids[start:stop],
            documents=documents[start:stop],
            metadatas=metadatas[start:stop],
            embeddings=embeddings[start:stop],
        )
        written += stop - start
        print(f"    batch {number}/{batches}: stored {written}", flush=True)
    return written


def count(collection=None) -> int:
    """Number of records in the collection."""
    collection = collection if collection is not None else open_collection()
    return collection.count()


def get_records(ids: list[str], include_embeddings: bool = True) -> dict:
    """Read specific records back, for the round-trip and metadata checks."""
    collection = open_collection()
    include = ["documents", "metadatas"]
    if include_embeddings:
        include.append("embeddings")
    return collection.get(ids=ids, include=include)


def query(embedding: list[float], top_k: int) -> dict:
    """Query the store for the nearest chunks. Read-only.

    The retrieval pipeline itself (relevance gate, prompt, LLM) is Phase 5. This
    is the thin store read that Phase 3 is required to prove works.
    """
    collection = open_collection()
    return collection.query(
        query_embeddings=[embedding],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )


def validate_store(collection=None) -> list[str]:
    """Return a list of provenance problems; empty means the store is usable."""
    problems: list[str] = []
    collection = collection if collection is not None else open_collection()
    meta = collection.metadata or {}
    for key, expected in collection_metadata().items():
        actual = meta.get(key)
        if actual != expected:
            problems.append(
                f"collection metadata {key}={actual!r}, expected {expected!r}"
            )
    return problems


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------
def chunking_config() -> dict[str, object]:
    """The Phase 2 chunking configuration, recorded for reproducibility."""
    return {
        "target_chars": config.CHUNK_TARGET_CHARS,
        "max_chars": config.CHUNK_MAX_CHARS,
        "overlap_sentences": config.CHUNK_OVERLAP_SENTENCES,
        "min_unit_chars": config.MIN_UNIT_CHARS,
        "sections": list(config.SECTION_PATTERNS),
        "drop_regions": [name for name, _, _ in config.DROP_REGIONS],
    }


def build_manifest(chunks, corpus_hash: str, source_rows: list[dict]) -> dict:
    """Assemble the manifest. Field list from doc/implementation.md step 7."""
    from src import chunker

    return {
        "schema_version": config.MANIFEST_SCHEMA_VERSION,
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # --- embedding provenance (architecture §10.3) ---
        "embedding_model_id": config.EMBEDDING_MODEL_ID,
        "embedding_dimension": config.EMBEDDING_DIMENSION,
        "normalize_embeddings": config.EMBEDDING_NORMALIZE,
        "embedding_device": config.EMBEDDING_DEVICE,
        "embedding_batch_size": config.EMBEDDING_BATCH_SIZE,
        "embedding_seed": config.EMBEDDING_SEED,
        "prompt_prefix": config.EMBEDDING_PROMPT_PREFIX,
        # --- store provenance ---
        "collection_name": config.COLLECTION_NAME,
        "distance_metric": config.DISTANCE_METRIC,
        "chroma_dir": str(config.CHROMA_DIR),
        # --- chunking provenance (Phase 2) ---
        "chunking": chunking_config(),
        # --- corpus ---
        "chunk_count": len(chunks),
        "corpus_hash": corpus_hash,
        "section_count": len({c.section for c in chunks}),
        "sections": sorted({c.section for c in chunks}),
        "sources": source_rows,
    }


def write_manifest(manifest: dict) -> None:
    config.MANIFEST_JSON.parent.mkdir(parents=True, exist_ok=True)
    config.MANIFEST_JSON.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def read_manifest() -> dict | None:
    """Read the manifest, or None if it has not been written."""
    if not config.MANIFEST_JSON.exists():
        return None
    try:
        return json.loads(config.MANIFEST_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def manifest_source_rows(documents) -> list[dict]:
    """Per-document provenance rows for the manifest."""
    return [
        {
            "slug": d.source.slug,
            "source_url": d.source.source_url,
            "page_name": d.source.page_name,
            "brief_label": d.source.brief_label,
            "doc_type": d.source.doc_type,
            "http_status": d.status,
            "fetched_at": d.fetched_at,
            "content_hash": d.content_hash,
            "unit_count": len(d.units),
        }
        for d in documents
    ]


__all__ = [
    "METADATA_FIELDS",
    "build_manifest",
    "chunking_config",
    "collection_metadata",
    "count",
    "drop_collection",
    "get_client",
    "get_or_create_collection",
    "get_records",
    "manifest_source_rows",
    "open_collection",
    "query",
    "read_manifest",
    "reset_store",
    "validate_store",
    "write_chunks",
    "write_manifest",
]
