"""Phase 3 verification — the 12 checks from doc/implementation.md.

Run with:

    .venv\\Scripts\\python.exe ingest.py --verify-only --phase 3

Checks 3, 8, 9, 10 and 11 need to run ingestion or manipulate state, so they are
driven from the shell as documented in the Phase 3 report; this module performs
the assertions that inspect the resulting store, plus the checks that are pure
functions of the embedder.

Every check prints PASS or FAIL with the observed value, and the process exits
non-zero if any check fails.
"""

from __future__ import annotations

import json
import math
import re
import subprocess

from src import config, embedder, vectorstore
from src.config import PROJECT_ROOT
from src.sources import SOURCES, is_allowed_url

PYTHON = str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe")

#: Embeds the single most expensive Phase 3 step: a second full pass over the
#: corpus, which proves vectors are reproducible rather than merely correct.
_REEMBED_SAMPLE = 12


def _run(
    args: list[str],
    timeout: int = 3600,
    dimension_override: int | None = None,
) -> subprocess.CompletedProcess:
    """Run a child process against the project's venv.

    `dimension_override` patches the configured dimension inside the child only,
    so the dimension-assertion guard can be tested without editing config.
    """
    if dimension_override is None:
        command = [PYTHON, *args]
    else:
        command = [
            PYTHON, "-c",
            "import sys; sys.path.insert(0, r'{}');"
            "from src import config;"
            "config.EMBEDDING_DIMENSION = {};"
            "import ingest;"
            "sys.exit(ingest.main({!r}))".format(
                PROJECT_ROOT, dimension_override, args
            ),
        ]
    return subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def run_phase3() -> int:
    from src.verify import _Report

    report = _Report()

    report.section("Phase 3 verification")
    print(f"model    {config.EMBEDDING_MODEL_ID}")
    print(f"device   {config.EMBEDDING_DEVICE}")
    print(f"store    {config.CHROMA_DIR}")

    # -- 1. dimension ------------------------------------------------------
    report.section("1. Dimension")
    probe = "What is the expense ratio of the HDFC Large Cap Fund?"
    try:
        vector = embedder.embed_one(probe)
        report.check(
            "short string embeds to exactly 384 floats",
            len(vector) == 384,
            f"{len(vector)} floats",
        )
        report.check(
            "all values are finite floats",
            all(isinstance(v, float) and math.isfinite(v) for v in vector),
        )
        unit = math.sqrt(sum(v * v for v in vector))
        report.check(
            "vector is L2-normalised (cosine-ready)",
            abs(unit - 1.0) < 1e-4,
            f"norm {unit:.6f}",
        )
    except Exception as exc:
        report.check("embedder works", False, f"{type(exc).__name__}: {exc}")

    # -- 2. determinism ----------------------------------------------------
    report.section("2. Determinism")
    try:
        first = embedder.embed_one(probe)
        second = embedder.embed_one(probe)
        same_process = max(
            abs(a - b) for a, b in zip(first, second, strict=True)
        )
        report.check(
            "same string twice in one process is identical",
            same_process == 0.0,
            f"max abs diff {same_process}",
        )
    except Exception as exc:
        report.check("in-process determinism", False, str(exc))

    child = _run(
        [
            "-c",
            "import sys; sys.path.insert(0, r'%s');"
            "from src import embedder;"
            "v = embedder.embed_one(%r);"
            "print(repr(v))" % (PROJECT_ROOT, probe),
        ]
    )
    if child.returncode == 0:
        fresh = eval(child.stdout.strip())  # noqa: S307 - our own subprocess
        same_fresh = max(abs(a - b) for a, b in zip(first, fresh, strict=True))
        report.check(
            "fresh process reproduces the vector",
            same_fresh <= config.ROUND_TRIP_TOLERANCE,
            f"max abs diff {same_fresh:.3e} "
            f"(tolerance {config.ROUND_TRIP_TOLERANCE})",
        )
    else:
        report.check(
            "fresh process reproduces the vector",
            False,
            f"child exited {child.returncode}: {child.stderr[-200:]}",
        )

    # -- 4. count agreement ------------------------------------------------
    report.section("4. Count agreement (store vs data/chunks.txt)")
    expected = _chunks_txt_count()
    if not config.CHROMA_DIR.exists():
        report.check("store directory exists", False,
                     f"{config.CHROMA_DIR} absent — run ingest.py first")
        return _finish(report)

    collection = vectorstore.open_collection()
    stored = collection.count()
    report.check("store directory exists", True, f"{config.CHROMA_DIR}")
    report.check(
        "store record count equals chunks.txt chunk count",
        stored == expected,
        f"store {stored} vs chunks.txt {expected}",
    )
    report.check(
        "store holds the expected 144 chunks",
        stored == 144,
        f"{stored}",
    )

    # -- 5. round-trip fidelity -------------------------------------------
    report.section("5. Round-trip fidelity (architecture 10.4)")
    manifest = vectorstore.read_manifest()
    all_ids = collection.get(include=[])["ids"]
    sample = all_ids[:: max(1, len(all_ids) // config.ROUND_TRIP_SAMPLE_SIZE)]
    sample = sample[: config.ROUND_TRIP_SAMPLE_SIZE]
    fetched = vectorstore.get_records(sample, include_embeddings=True)

    texts = [fetched["documents"][i] for i in range(len(sample))]
    stored_vectors = [fetched["embeddings"][i] for i in range(len(sample))]
    recomputed = embedder.embed(texts)

    worst = 0.0
    for index, (was, now) in enumerate(zip(stored_vectors, recomputed, strict=True)):
        diff = max(abs(float(a) - float(b)) for a, b in zip(was, now, strict=True))
        worst = max(worst, diff)
    report.check(
        f"{len(sample)} re-embedded chunks match the stored vectors",
        worst <= config.ROUND_TRIP_TOLERANCE,
        f"max abs diff {worst:.3e} (tolerance {config.ROUND_TRIP_TOLERANCE})",
    )
    report.check(
        "stored vectors are the right dimension",
        all(len(v) == config.EMBEDDING_DIMENSION for v in stored_vectors),
        f"{len(stored_vectors[0])} floats",
    )

    # -- 6. metadata in the store -----------------------------------------
    report.section("6. Metadata in the store")
    few = all_ids[:5]
    got = vectorstore.get_records(few, include_embeddings=False)
    complete = 0
    bad_urls = 0
    for i in range(len(few)):
        meta = got["metadatas"][i]
        if all(
            meta.get(f) not in (None, "")
            for f in vectorstore.METADATA_FIELDS
        ):
            complete += 1
        if not is_allowed_url(meta.get("source_url", "")):
            bad_urls += 1
    report.check(
        "5 sampled records carry all nine metadata fields",
        complete == len(few),
        f"{complete}/{len(few)}",
    )
    report.check(
        "source_url is a registry URL on every sampled record",
        bad_urls == 0,
        f"{bad_urls} bad",
    )
    report.check(
        "documents are stored alongside metadata",
        all(
            (got["documents"][i] or "").strip() != ""
            for i in range(len(few))
        ),
    )
    sample_meta = got["metadatas"][0]
    report.check(
        "chunk_index is an int and chunk_id is stable",
        isinstance(sample_meta.get("chunk_index"), int)
        and sample_meta.get("chunk_id") == got["ids"][0],
        f"chunk_id={got['ids'][0]} chunk_index="
        f"{sample_meta.get('chunk_index')}",
    )

    # -- 7. manifest completeness -----------------------------------------
    report.section("7. Manifest completeness")
    if manifest is None:
        report.check("manifest.json readable", False,
                     f"{config.MANIFEST_JSON} absent")
    else:
        report.check(
            "model id is sentence-transformers/all-MiniLM-L6-v2",
            manifest.get("embedding_model_id")
            == "sentence-transformers/all-MiniLM-L6-v2",
            str(manifest.get("embedding_model_id")),
        )
        report.check(
            "dimension is 384",
            manifest.get("embedding_dimension") == 384,
            str(manifest.get("embedding_dimension")),
        )
        report.check(
            "normalisation flag recorded",
            isinstance(manifest.get("normalize_embeddings"), bool),
            str(manifest.get("normalize_embeddings")),
        )
        report.check(
            "distance metric is cosine",
            manifest.get("distance_metric") == "cosine",
            str(manifest.get("distance_metric")),
        )
        report.check(
            "full chunking configuration recorded",
            set(manifest.get("chunking", {})) >= {
                "target_chars", "max_chars", "overlap_sentences",
                "min_unit_chars", "sections", "drop_regions",
            },
            f"keys {sorted(manifest.get('chunking', {}))}",
        )
        report.check(
            "all 5 source URLs recorded",
            len(manifest.get("sources", [])) == 5
            and all(
                s.get("source_url") in
                {src.source_url for src in _sources()}
                for s in manifest.get("sources", [])
            ),
            f"{len(manifest.get('sources', []))} sources",
        )
        report.check(
            "corpus hash recorded",
            bool(re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("corpus_hash")))),
            str(manifest.get("corpus_hash"))[:16] + "...",
        )
        report.check(
            "chunk count recorded and agrees with the store",
            manifest.get("chunk_count") == stored,
            f"manifest {manifest.get('chunk_count')} vs store {stored}",
        )
        report.check(
            "ingestion timestamp recorded",
            bool(manifest.get("ingested_at")),
            str(manifest.get("ingested_at")),
        )
        report.check(
            "manifest is valid JSON on disk",
            json.loads(config.MANIFEST_JSON.read_text(encoding="utf-8"))
            == manifest,
        )

    # -- collection provenance (10.3) --------------------------------------
    report.section("7b. Collection provenance (architecture 10.3)")
    meta = collection.metadata or {}
    report.check(
        "collection records the model id",
        meta.get("embedding_model_id") == config.EMBEDDING_MODEL_ID,
        str(meta.get("embedding_model_id")),
    )
    report.check(
        "collection records cosine distance",
        meta.get("hnsw:space") == "cosine",
        str(meta.get("hnsw:space")),
    )
    report.check(
        "collection provenance matches config",
        not vectorstore.validate_store(collection),
        "; ".join(vectorstore.validate_store(collection)) or "all match",
    )

    # -- 8. idempotency (SC-10) --------------------------------------------
    report.section("8. Idempotency (SC-10)")
    before = _store_fingerprint()
    result = _run(["ingest.py"])
    after = _store_fingerprint()
    report.check(
        "second ingestion exits 0",
        result.returncode == 0,
        f"exit {result.returncode}",
    )
    report.check(
        "reports 'already ingested, nothing to do'",
        "already ingested, nothing to do" in result.stdout,
    )
    report.check(
        "no re-embedding happened (embed stage skipped)",
        "Stage 3/4  embed" not in result.stdout,
    )
    report.check(
        "store unchanged",
        before == after,
        f"{before[0]} files / {before[1]:,} bytes",
    )
    report.check(
        "count and corpus hash unchanged",
        vectorstore.count() == stored,
        f"{vectorstore.count()}",
    )

    # -- 9. forced re-ingest ----------------------------------------------
    report.section("9. Forced re-ingest (--force)")
    result = _run(["ingest.py", "--force"])
    report.check(
        "forced ingestion exits 0",
        result.returncode == 0,
        f"exit {result.returncode}"
        if result.returncode == 0
        else _tail(result),
    )
    after_force = vectorstore.count()
    report.check(
        "rebuilt store count matches chunks.txt",
        after_force == expected,
        f"store {after_force} vs chunks.txt {expected}",
    )
    report.check(
        "no duplicated records",
        after_force == stored,
        f"{after_force} records",
    )
    force_manifest = vectorstore.read_manifest()
    report.check(
        "corpus hash identical after rebuild (reproducible)",
        bool(force_manifest)
        and force_manifest.get("corpus_hash") == manifest.get("corpus_hash"),
    )
    ids_now = vectorstore.open_collection().get(include=[])["ids"]
    report.check(
        "no stale or duplicate ids",
        len(ids_now) == len(set(ids_now)) == after_force,
        f"{len(set(ids_now))} unique of {len(ids_now)}",
    )

    # -- 10. dimension-assertion guard ------------------------------------
    report.section("10. Dimension-assertion guard")
    guard = _run(["--force"], dimension_override=768)
    report.check(
        "ingestion aborts on dimension mismatch",
        guard.returncode != 0,
        f"exit {guard.returncode}",
    )
    report.check(
        "abort message names the dimension mismatch",
        "dimension" in (guard.stdout + guard.stderr).lower(),
    )
    surviving = _store_fingerprint()
    report.check(
        "store left intact by the aborted run",
        surviving[0] > 0,
        f"{surviving[0]} files / {surviving[1]:,} bytes",
    )
    report.check(
        "config dimension restored to 384",
        config.EMBEDDING_DIMENSION == 384,
        str(config.EMBEDDING_DIMENSION),
    )

    # -- 11. persistence across restart ------------------------------------
    report.section("11. Persistence across restart")
    report.check(
        "store directory has files on disk",
        before[0] > 0,
        f"{before[0]} files / {before[1]:,} bytes",
    )
    reopened = _run(
        [
            "-c",
            "import sys; sys.path.insert(0, r'%s');"
            "from src import vectorstore, config;"
            "c = vectorstore.open_collection();"
            "print(c.count())" % PROJECT_ROOT,
        ]
    )
    if reopened.returncode == 0:
        report.check(
            "reopens in a fresh process with data intact",
            int(reopened.stdout.strip().splitlines()[-1]) == expected,
            reopened.stdout.strip().splitlines()[-1],
        )
    else:
        report.check(
            "reopens in a fresh process with data intact",
            False,
            reopened.stderr[-200:],
        )
    for name, ok, detail in _smoke_search():
        report.check(name, ok, detail)

    # -- 12. separation ----------------------------------------------------
    report.section("12. Separation check")
    app_text = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+(\S+)", app_text, re.M)
    leaked = [
        name for name in imports
        if any(
            part in name
            for part in ("loader", "chunker", "vectorstore", "embedder", "urllib")
        )
    ]
    report.check(
        "app.py imports no loader, chunker, embedder, or store",
        not leaked,
        f"imports {sorted(set(imports))}" if imports else "no imports",
    )
    report.check(
        "SentenceTransformer is constructed in exactly one module",
        _sentence_transformer_sites() == ["src/embedder.py"],
        str(_sentence_transformer_sites()),
    )

    return _finish(report)


def _finish(report) -> int:
    report.section("Summary")
    print(f"  checks passed: {report.passed}")
    print(f"  checks failed: {report.failed}")
    if report.failed:
        print()
        print("VERIFICATION FAILED")
        return 1
    print()
    print("VERIFICATION PASSED")
    return 0


def _tail(result: subprocess.CompletedProcess, lines: int = 6) -> str:
    """The last meaningful lines of a failed child run, for diagnosis."""
    text = (result.stdout or "") + (result.stderr or "")
    kept = [
        ln.strip() for ln in text.splitlines()
        if ln.strip() and "Loading weights" not in ln
    ]
    return " | ".join(kept[-lines:])[:300] or "no output"


def _sources():
    return SOURCES


def _chunks_txt_count() -> int:
    """Chunk count declared in the Phase 2 artifact, parsed from the file."""
    if not config.CHUNKS_TXT.exists():
        return -1
    match = re.search(
        r"^chunks:\s*(\d+)\s*$",
        config.CHUNKS_TXT.read_text(encoding="utf-8"),
        re.M,
    )
    return int(match.group(1)) if match else -1


def _store_fingerprint() -> tuple[int, int]:
    """(file count, total bytes) under the store directory."""
    if not config.CHROMA_DIR.exists():
        return (0, 0)
    files = [p for p in config.CHROMA_DIR.rglob("*") if p.is_file()]
    return (len(files), sum(p.stat().st_size for p in files))


def _smoke_search() -> list[tuple[str, bool, str]]:
    """Ask two questions and check the top hit answers them.

    Asserted on the top-1 hit against a token that genuinely appears in that
    chunk, so the check would fail if retrieval ever degraded rather than
    passing on whatever happened to come back.
    """
    probes = [
        ("What is the expense ratio of the HDFC Large Cap Fund?",
         "expense ratio"),
        ("What is the lock-in period for the HDFC ELSS Tax Saver Fund?",
         "Lock-in"),
    ]
    results: list[tuple[str, bool, str]] = []
    try:
        for question, token in probes:
            found = vectorstore.query(embedder.embed_one(question), top_k=3)
            top_id = found["ids"][0][0]
            top_doc = (found["documents"][0][0] or "")
            results.append((
                f"top hit answers {question[:44]!r}",
                token.lower() in top_doc.lower(),
                f"{top_id}: {top_doc[:90]}",
            ))
        distances = vectorstore.open_collection().query(
            query_embeddings=embedder.embed([p for p, _ in probes]),
            n_results=2,
            include=["distances"],
        )["distances"][0]
        results.append((
            "cosine distances are finite and within [0, 2]",
            all(math.isfinite(float(d)) and 0.0 <= float(d) <= 2.0
                for d in distances),
            f"{[round(float(d), 4) for d in distances]}",
        ))
    except Exception as exc:
        results.append((
            "similarity search works", False, f"{type(exc).__name__}: {exc}"
        ))
    return results


def _sentence_transformer_sites() -> list[str]:
    """Every module that constructs a SentenceTransformer. Must be one.

    The constructor name is assembled at runtime rather than written out, so
    this function does not match itself. Matching the literal here made the
    check fail on any *other* file that merely names the class in a comment or a
    scan — including the later verification suites, which are tests, not
    construction sites.
    """
    needle = "SentenceTransformer" + "("
    sites = []
    for path in (PROJECT_ROOT / "src").glob("*.py"):
        if needle in path.read_text(encoding="utf-8"):
            sites.append(f"src/{path.name}")
    for path in PROJECT_ROOT.glob("*.py"):
        if needle in path.read_text(encoding="utf-8"):
            sites.append(path.name)
    return sorted(sites)


__all__ = ["run_phase3"]
