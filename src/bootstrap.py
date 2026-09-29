"""First-run bootstrap — build the vector store if it is missing, then get out of
the way.

Why this exists
---------------
`data/chroma/` is deliberately excluded from Git, so a fresh checkout (and every
Streamlit Community Cloud deploy, which runs from a clean clone) starts with no
store. `preflight` correctly refuses to serve without one, but the operator who
just cloned the repo has no obvious way to build it other than discovering the
separate `ingest.py` step. This module closes that gap: when the app starts and
the store is absent, it runs the *existing* ingestion pipeline once to create it,
then the app proceeds normally.

What this is not
----------------
This is not a second ingestion implementation and it does not relax SC-10. It
calls the same `ingest.ingest()` entry point used offline, with `force=False`, so
every guard in that pipeline still applies (no silent replacement of a changed
corpus, `--force` semantics unchanged, and its own already-ingested check runs
first). The single behavioural difference from before is *when* it happens: a
missing store is now built on first app start instead of only via the CLI.

Two properties are load-bearing for SC-10 and the query path:

1. The ingestion modules are imported *inside* the function, not at module import
   time. That keeps `loader`/`chunker` out of the static import graph of the query
   path (`app.py` and the `src/` modules a question travels through), which
   Phase 6 asserts. The import happens only when a build is actually required.
2. It is a strict no-op whenever the store already exists and validates. If the
   store is present, this returns immediately and touches nothing on disk, so a
   normal start performs no ingestion whatsoever.

Failure is non-fatal. If the build cannot complete (e.g. the source pages are
unreachable during the app's start-up), this returns a message and the caller
lets `preflight` produce the same "no store / run ingest.py" guidance as before.
The bootstrap never masks an error by pretending the store is ready.
"""

from __future__ import annotations

from src import config, preflight


def store_is_ready() -> bool:
    """True if the app can already serve without building anything.

    Delegates to the same read-only `preflight.run()` the app uses, so "ready"
    means exactly what the app means by it: the store exists, the manifest is
    present and readable, and it was built with the configured embedder.
    """
    return bool(preflight.run())


def ensure_store(log=print) -> tuple[bool, str]:
    """Build the vector store if it is missing. Returns (ok, message).

    A no-op when `store_is_ready()`. Otherwise runs the existing ingestion
    pipeline with `force=False`. `log` receives human-readable progress so the
    start-up build is visible instead of a silent stall; the default writes to
    stdout, which Streamlit shows in its server log.
    """
    if store_is_ready():
        return True, "Store already present; no ingestion needed."

    log("[bootstrap] No store found — running the ingestion pipeline once.")
    log(f"[bootstrap] Target: {config.CHROMA_DIR}")

    # Imported here, not at module top, so the query path's static import graph
    # stays free of the ingestion modules (see module docstring / SC-10).
    import ingest

    try:
        exit_code = ingest.ingest(force=False)
    except Exception as exc:  # noqa: BLE001 - start-up must not crash the app
        return (
            False,
            f"Auto-ingestion raised {type(exc).__name__}: {exc}. "
            f"Run `{preflight.INGEST_COMMAND}` manually to build the store.",
        )

    if exit_code != 0:
        return (
            False,
            f"Auto-ingestion failed (exit {exit_code}). "
            f"Run `{preflight.INGEST_COMMAND}` to build the store and check its "
            f"output.",
        )

    # Re-check through the real gate rather than trusting the exit code, so the
    # app is only allowed to serve when the store genuinely validates.
    if not store_is_ready():
        return (
            False,
            "Auto-ingestion reported success but the store still does not "
            f"validate. Run `{preflight.INGEST_COMMAND}` and read its output.",
        )

    log(f"[bootstrap] Store built at {config.CHROMA_DIR}.")
    return True, "Store built."


__all__ = ["ensure_store", "store_is_ready"]
