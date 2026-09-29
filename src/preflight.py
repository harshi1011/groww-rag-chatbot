"""Startup preflight (architecture §9) — cheap checks, no ingestion, ever.

The store, manifest, and model id are validated before the app serves a single
question. The reason this matters: a silent mismatch between ingestion-time and
query-time embeddings does not error. It returns confidently wrong neighbours,
and the model then answers from irrelevant context while appearing healthy. So
a mismatch refuses to serve rather than warns and continues (SC-10, §10.1).

The preflight never ingests, never fetches, and never repairs. Its job is to say
what the operator must run, and stop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from src import config, sources, vectorstore

INGEST_COMMAND = "python ingest.py"


@dataclass(frozen=True)
class Preflight:
    """Whether the query pipeline may serve, and why not if it may not."""

    ok: bool
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    message: str = ""

    def __bool__(self) -> bool:
        return self.ok


def run(
    chroma_dir=None, manifest_path=None, expected_model_id: str | None = None
) -> Preflight:
    """Check the four preconditions in architecture §9, in order.

    Check 4 is a URL-level comparison against the source registry rather than a
    `corpus_hash` recomputation. Recomputing the hash requires re-fetching and
    re-chunking, which is ingestion — the exact thing query time must not do. A
    registry entry that was added, removed, or had its URL changed is still
    detected, which is the drift that would change the corpus.
    """
    problems: list[str] = []
    warnings: list[str] = []

    chroma_dir = chroma_dir if chroma_dir is not None else config.CHROMA_DIR
    manifest_path = (
        manifest_path if manifest_path is not None else config.MANIFEST_JSON
    )
    expected_model_id = (
        expected_model_id
        if expected_model_id is not None
        else config.EMBEDDING_MODEL_ID
    )

    # 1. Does the store exist?
    if not chroma_dir.is_dir():
        problems.append(
            f"No store at {chroma_dir}. Ingestion has not been run. "
            f"Run: {INGEST_COMMAND}"
        )
        return _result(problems, warnings)

    # 2. Does the manifest exist?
    if not manifest_path.is_file():
        problems.append(
            f"No manifest at {manifest_path}. Ingestion has not been run. "
            f"Run: {INGEST_COMMAND}"
        )
        return _result(problems, warnings)

    # 3. Model id match. This one refuses rather than warns: serving with a
    #    different embedder than the one the store was built with produces
    #    nonsense neighbours with no visible symptom.
    manifest = _read_manifest(manifest_path)
    if not manifest:
        problems.append(
            f"Manifest at {manifest_path} is unreadable or empty. "
            f"Run: {INGEST_COMMAND}"
        )
        return _result(problems, warnings)

    manifest_model = manifest.get("embedding_model_id", "")
    if manifest_model != expected_model_id:
        problems.append(
            "Embedding model mismatch: the store was built with "
            f"{manifest_model!r} but the app is configured for "
            f"{expected_model_id!r}. Refusing to serve, because queries would be "
            f"embedded differently from the documents. Re-run: {INGEST_COMMAND}"
        )
        return _result(problems, warnings)

    # 4. Registry drift. A warning, not a refusal: the existing corpus is still
    #    internally consistent and answerable, it is just possibly not current.
    registry_urls = {source.source_url for source in sources.SOURCES}
    ingested_urls = {
        row.get("source_url", "") for row in manifest.get("sources", [])
    }
    ingested_urls.discard("")
    added = sorted(registry_urls - ingested_urls)
    removed = sorted(ingested_urls - registry_urls)
    if added or removed:
        warnings.append(
            "The source registry has changed since the last ingestion "
            f"(new: {len(added)}, missing: {len(removed)}). Re-run "
            f"`{INGEST_COMMAND}` to pick up the changes. Answers will use the "
            "previous snapshot until then."
        )

    return _result(problems, warnings)


def _read_manifest(manifest_path):
    """Read the manifest at the given path, not the configured one.

    `vectorstore.read_manifest` is hard-wired to the configured path, so the
    preflight needs its own reader in order to be testable against a modified
    manifest without touching the real artefact.
    """
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _result(problems: list[str], warnings: list[str]) -> Preflight:
    if problems:
        return Preflight(
            ok=False,
            problems=tuple(problems),
            warnings=tuple(warnings),
            message=problems[0],
        )
    return Preflight(ok=True, warnings=tuple(warnings))


__all__ = ["INGEST_COMMAND", "Preflight", "run"]
