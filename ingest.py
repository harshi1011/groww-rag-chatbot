"""Phase 2/3 — ingestion: fetch, normalise, chunk, embed, store.

This entry point runs once, offline. It performs the full
load -> chunk -> embed -> store pipeline and emits the manifest.

Idempotency (SC-10, architecture §2.6): if the store exists and the manifest's
corpus hash matches the corpus just built, ingestion exits as an explicit no-op.
Re-ingesting a *changed* corpus requires `--force`, so state is never silently
replaced. `--force` deletes the store directory first, so a rebuild cannot leave
duplicated or stale records behind.

The query path in `app.py` must never import this file (architecture §9).
"""

from __future__ import annotations

import argparse
import sys
import time

from src import chunker, config, embedder, loader, vectorstore
from src.sources import SOURCES


def _print_header(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def _print_registry() -> None:
    _print_header("Ingestion targets")
    print(f"Registry: {len(SOURCES)} sources")
    for source in SOURCES:
        print(f"  - {source.page_name}")
        print(f"    {source.source_url}")


def _print_documents(documents, failures) -> None:
    _print_header("Stage 1/4  load")
    for document in documents:
        print(
            f"  HTTP {document.status}  {document.source.page_name}"
            f"  ({document.html_bytes:,} bytes, {document.raw_line_count} visible"
            f" lines, {len(document.units)} units, hash {document.content_hash})"
        )
    for url, error in failures:
        print(f"  FAILED  {url}: {error}")


def _already_ingested(corpus_hash: str) -> bool:
    """True if the store on disk was built from exactly this corpus.

    Every condition must hold: the manifest exists and is for the same schema
    and model, the corpus hash matches, the collection exists with the pinned
    provenance, and the record count matches the manifest. Anything less is
    treated as "needs rebuild" rather than assumed good.
    """
    manifest = vectorstore.read_manifest()
    if manifest is None:
        return False
    if manifest.get("schema_version") != config.MANIFEST_SCHEMA_VERSION:
        return False
    if manifest.get("embedding_model_id") != config.EMBEDDING_MODEL_ID:
        return False
    if manifest.get("corpus_hash") != corpus_hash:
        return False
    if not config.CHROMA_DIR.exists():
        return False
    try:
        collection = vectorstore.open_collection()
    except Exception:
        return False
    if vectorstore.validate_store(collection):
        return False
    return collection.count() == manifest.get("chunk_count")


def _write_chunks_txt(chunks, documents) -> None:
    """Write the human-readable chunk artifact.

    Deliberately plain text, not JSON: the point of this file is that a reviewer
    can read every chunk and its metadata without running anything. The
    machine-readable manifest is written alongside it.
    """
    lines: list[str] = []
    lines.append("# HDFC Mutual Fund RAG - chunked corpus (Phase 2)")
    lines.append("#")
    lines.append("# Human-readable, one chunk per block. Every chunk carries the")
    lines.append("# source URL it came from, so any statement can be traced back.")
    lines.append("# Regenerate with: .venv\\Scripts\\python.exe ingest.py")
    lines.append("")
    lines.append(f"documents: {len(documents)}")
    lines.append(f"chunks: {len(chunks)}")
    lines.append("")

    for document in documents:
        lines.append("=" * 78)
        lines.append(f"SOURCE  {document.source.page_name}")
        lines.append(f"URL     {document.source.source_url}")
        lines.append(f"FETCHED {document.fetched_at}")
        lines.append(f"HASH    {document.content_hash}")
        lines.append("=" * 78)
        lines.append("")

    by_url: dict[str, list[chunker.Chunk]] = {}
    for chunk in chunks:
        by_url.setdefault(chunk.source_url, []).append(chunk)

    for document in documents:
        lines.append("")
        lines.append("#" * 78)
        lines.append(f"# {document.source.page_name}")
        lines.append("#" * 78)
        for chunk in by_url.get(document.source.source_url, []):
            lines.append("")
            lines.append(f"[{chunk.chunk_id}] section: {chunk.section}")
            lines.append(f"    {chunk.text}")
        lines.append("")

    config.CHUNKS_TXT.parent.mkdir(parents=True, exist_ok=True)
    config.CHUNKS_TXT.write_text("\n".join(lines), encoding="utf-8")


def ingest(force: bool = False) -> int:
    """Run the pipeline. Returns a process exit code."""
    _print_registry()
    problems = config.validate_ingestion_config()
    if problems:
        _print_header("Configuration problems")
        for problem in problems:
            print(f"  {problem}")
        return 1

    # --- stage 1: load -----------------------------------------------------
    started = time.perf_counter()
    documents, document_failures = loader.load_all()
    _print_documents(documents, document_failures)

    if document_failures:
        print()
        print(
            f"{len(document_failures)} source(s) failed. Refusing to write a "
            f"partial corpus."
        )
        return 1
    if len(documents) != len(SOURCES):
        print()
        print(
            f"Expected {len(SOURCES)} documents, got {len(documents)}. "
            f"Refusing to continue."
        )
        return 1

    # --- stage 2: chunk ----------------------------------------------------
    _print_header("Stage 2/4  chunk")
    chunks = chunker.build_corpus_chunks(documents)
    corpus_hash = chunker.corpus_hash(chunks)
    print(f"  {len(chunks)} chunks from {len(documents)} documents")
    for document in documents:
        mine = [c for c in chunks if c.source_url == document.source.source_url]
        print(
            f"  {document.source.page_name}: {len(mine)} chunks"
            f" | {len({c.section for c in mine})} sections"
        )
    print(f"  corpus hash {corpus_hash}")

    # --- SC-10: persist and skip ------------------------------------------
    if not force and _already_ingested(corpus_hash):
        _print_header("Already ingested")
        manifest = vectorstore.read_manifest()
        print("  already ingested, nothing to do.")
        print(f"  corpus hash   : {corpus_hash} (unchanged)")
        print(f"  chunk count   : {manifest.get('chunk_count')} (unchanged)")
        print(f"  store         : {config.CHROMA_DIR}")
        print(f"  model         : {manifest.get('embedding_model_id')}")
        print(f"  ingested at   : {manifest.get('ingested_at')}")
        print()
        print("  Use --force to rebuild the store from scratch.")
        return 0

    if not force and config.CHROMA_DIR.exists():
        previous = vectorstore.read_manifest()
        if previous is not None and previous.get("corpus_hash") != corpus_hash:
            _print_header("Store is stale")
            print("  A store exists but was built from a different corpus:")
            print(f"    on disk : {previous.get('corpus_hash')}")
            print(f"    now     : {corpus_hash}")
            print()
            print("  Refusing to replace it silently (SC-10). Re-run with --force")
            print("  to rebuild the store from scratch.")
            return 1

    # --- stage 3: embed ----------------------------------------------------
    _print_header("Stage 3/4  embed")
    print(f"  model    {config.EMBEDDING_MODEL_ID}")
    print(f"  device   {config.EMBEDDING_DEVICE}")
    print(f"  expected dimension {config.EMBEDDING_DIMENSION}")
    print(f"  normalize_embeddings {config.EMBEDDING_NORMALIZE}")
    embed_started = time.perf_counter()
    try:
        embeddings = embedder.embed(
            [c.text for c in chunks], show_progress=False
        )
    except Exception as exc:
        # The dimension guard lives in the embedder; surface it as an abort with
        # nothing written, which is what verification step 10 requires.
        print()
        print(f"  EMBEDDING ABORTED: {exc}")
        return 1
    embed_seconds = time.perf_counter() - embed_started
    print(
        f"  embedded {len(embeddings)} chunks in {embed_seconds:.1f}s"
        f" (dimension {len(embeddings[0])})"
    )

    # --- stage 4: store ----------------------------------------------------
    _print_header("Stage 4/4  store")
    if force:
        print("  --force: removing the previous store for a clean rebuild")
        vectorstore.reset_store()
    written = vectorstore.write_chunks(chunks, embeddings)
    stored = vectorstore.count()
    print(f"  {written} records written to {config.CHROMA_DIR}")
    print(f"  collection {config.COLLECTION_NAME!r} ({config.DISTANCE_METRIC})"
          f" now holds {stored} records")

    store_problems = vectorstore.validate_store()
    if store_problems:
        for problem in store_problems:
            print(f"  STORE PROBLEM: {problem}")
        return 1

    _print_header("Artifacts")
    _write_chunks_txt(chunks, documents)
    manifest = vectorstore.build_manifest(
        chunks,
        corpus_hash,
        vectorstore.manifest_source_rows(documents),
    )
    vectorstore.write_manifest(manifest)
    print(f"  {config.CHUNKS_TXT}  ({config.CHUNKS_TXT.stat().st_size:,} bytes)")
    print(f"  {config.MANIFEST_JSON}  "
          f"({config.MANIFEST_JSON.stat().st_size:,} bytes)")

    print()
    print(
        f"Ingestion complete in {time.perf_counter() - started:.1f}s: "
        f"{len(documents)} documents, {len(chunks)} chunks, {stored} vectors."
    )
    print("No guardrails, LLM, or UI work was done (Phases 4-6).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingestion: fetch, normalise, chunk, embed, store."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete the existing store and rebuild it from scratch.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Run verification and write nothing.",
    )
    parser.add_argument(
        "--phase",
        choices=("2", "3", "4", "5", "6", "all"),
        default="2",
        help="Which verification phase to run with --verify-only (default: 2).",
    )
    args = parser.parse_args(argv)

    if args.verify_only:
        from src import verify

        if args.phase == "2":
            return verify.run_phase2()
        if args.phase == "3":
            return verify.run_phase3()
        if args.phase == "4":
            return verify.run_phase4()
        if args.phase == "5":
            return verify.run_phase5()
        if args.phase == "6":
            return verify.run_phase6()
        failed = verify.run_phase2()
        failed = verify.run_phase3() or failed
        failed = verify.run_phase4() or failed
        failed = verify.run_phase5() or failed
        failed = verify.run_phase6() or failed
        return failed

    return ingest(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
