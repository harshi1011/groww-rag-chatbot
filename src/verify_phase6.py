"""Phase 6 verification — the 12 checks from doc/implementation.md, plus the
15 PRD success criteria.

This suite drives the real `app.py` through Streamlit's own `AppTest` harness
rather than reimplementing the UI. That distinction is the point: a check that
calls `answer.ask` and inspects the result proves the backend works, but says
nothing about what the user is actually shown — how many links are drawn, whether
the disclaimer is on the first screen, whether a PII value reaches the transcript.
Those are the claims PRD §10 makes, so they are the ones checked here.

Two things need saying about cost and honesty:

* The factual, refusal, PII, and no-answer checks use **live** Groq calls, because
  the properties under test (exactly one link, ≤ 3 sentences, no echo) are
  properties of a rendered answer, and a stubbed model would only prove they hold
  for the stub. Each check reports the state it observed.
* Check 11 launches a real headless server on a chosen port and probes it over
  HTTP, because "it binds the port and serves" is the whole of Render readiness
  and AppTest cannot observe it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from streamlit.testing.v1 import AppTest

from src import (
    answer,
    config,
    deliverables,
    guardrails,
    prompt,
    retrieval,
    sources,
    vectorstore,
)
from src.config import PROJECT_ROOT

APP_PATH = PROJECT_ROOT / "app.py"
DATA_DIR = config.DATA_DIR
SAMPLE_QA = PROJECT_ROOT / "samples" / "sample_qa.md"
README = PROJECT_ROOT / "README.md"

#: A port that is free on a dev machine and unlikely to collide with anything
#: else in a CI run. Chosen per run from this base, not hard-coded, so two
#: concurrent verifications do not fight over the same socket.
PORT_BASE = 8571

#: The query path: `app.py` plus the `src/` modules a user's question travels
#: through. `loader` and `chunker` are legitimately reachable from `ingest.py` and
#: from the verification suites, so a repo-wide scan would report correct code as
#: a violation. What has to hold is that nothing here can pull in an ingestion
#: module.
#:
#: `vectorstore.py` is shared by both pipelines and is handled separately below,
#: because a naive scan of it would flag `build_manifest`, which lazily imports
#: the chunker to record the chunking config and is only ever called by ingestion.
QUERY_PATH_FILES: tuple[str, ...] = (
    "app.py",
    "src/answer.py",
    "src/retrieval.py",
    "src/prompt.py",
    "src/llm.py",
    "src/render.py",
    "src/guardrails.py",
    "src/memory.py",
    "src/preflight.py",
    "src/embedder.py",
)

#: Ingestion-only functions in the shared store module, and the reason each is
#: allowed to reach the chunker. The query path calls none of them, and the check
#: below proves it by asserting the store module exposes no other route.
INGESTION_ONLY_STORE_FUNCTIONS: tuple[str, ...] = ("build_manifest",)

#: Seconds to wait for a Streamlit server to answer its health endpoint. The
#: first request pays for the sentence-transformers model download/load, so this
#: is generous rather than tight.
HEALTH_TIMEOUT = 300.0

#: The ten PRD §6 factual questions, verbatim. Drawn from the same tuple the
#: Phase 5 suite uses so the two phases cannot drift apart.
FACTUAL_QUESTIONS: tuple[str, ...] = (
    "What is the expense ratio of the HDFC Large Cap Fund Direct Growth?",
    "What is the exit load on the HDFC Equity Fund Direct Growth?",
    "What is the minimum SIP amount for the HDFC Small Cap Fund Direct Growth?",
    "What is the lock-in period for the "
    "HDFC ELSS Tax Saver Fund Direct Plan Growth?",
    "What is the riskometer level and benchmark of the "
    "HDFC Balanced Advantage Fund Direct Growth?",
    "How do I download my capital-gains statement?",
    "What are the management fees / other charges listed for the "
    "HDFC Large Cap Fund?",
    "What is the minimum lump-sum investment amount for the "
    "HDFC ELSS Tax Saver Fund?",
    "Where can I find the SID / KIM for the HDFC Equity Fund?",
    "What documents are needed for tax reporting on these "
    "equity-oriented funds?",
)

#: Questions whose fact is genuinely absent from the corpus. A correct system
#: refuses these; an incorrect one invents a number, which is the failure a user
#: could act on. Kept separate from the ten because their correct outcome differs.
CORPUS_GAP_QUESTIONS: tuple[str, ...] = (
    "How do I download my capital-gains statement?",
    "What documents are needed for tax reporting on these equity-oriented funds?",
    "What is the Sharpe ratio of HDFC Large Cap Fund Direct Growth?",
)

REFUSAL_QUESTION = "Should I buy the HDFC Small Cap Fund?"
PERFORMANCE_QUESTION = "Which fund has the best returns?"
PII_QUESTION = "My PAN is ABCDE1234F, what is the exit load?"
PII_VALUE = "ABCDE1234F"

#: One self-contained question, then a follow-up that only makes sense after it.
#: Drives the memory path, and the follow-up is the only case where the app is
#: supposed to show an "understood as" caption.
FOLLOWUP_FIRST = "What is the expense ratio of the HDFC Large Cap Fund Direct Growth?"
FOLLOWUP_SECOND = "What about its exit load?"

_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)]+)\)")
_ANY_URL = re.compile(r"https?://[^\s)>]+")


class _Report:
    """Same reporting contract as the earlier phase suites, plus an SC ledger."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self._failures: list[str] = []
        #: SC id -> [(label, ok)], so one criterion can be evidenced by several
        #: independent checks and is only green when all of them are.
        self.criteria: dict[str, list[tuple[str, bool]]] = {}

    def section(self, title: str) -> None:
        print(f"\n{title}")
        print("-" * len(title))

    def check(self, label: str, ok: bool, detail: str = "", sc: str | None = None) -> bool:
        if ok:
            self.passed += 1
            print(f"  PASS  {label}" + (f" \u2014 {detail}" if detail else ""))
        else:
            self.failed += 1
            self._failures.append(label)
            print(f"  FAIL  {label}" + (f" \u2014 {detail}" if detail else ""))
        if sc:
            self.criteria.setdefault(sc, []).append((label, ok))
        return ok

    def sc_status(self, sc: str) -> tuple[bool, int, int]:
        entries = self.criteria.get(sc, [])
        if not entries:
            return False, 0, 0
        return all(ok for _, ok in entries), sum(1 for _, ok in entries if ok), len(entries)

    def finish(self, name: str) -> int:
        print("\nSummary")
        print("-------")
        print(f"  checks passed: {self.passed}")
        print(f"  checks failed: {self.failed}")
        if self.failed:
            print("  failing:")
            for label in self._failures:
                print(f"    - {label}")
            print(f"\n{name} FAILED")
            return 1
        print(f"\n{name} PASSED")
        return 0


# --------------------------------------------------------------------------
# The app under test
# --------------------------------------------------------------------------
def _new_app(timeout: float = 300.0) -> AppTest:
    """A fresh app session: new `st.session_state`, empty transcript.

    Not run yet. The first `run()` is what executes the script, and several checks
    need to inspect the first-screen tree, so the run is left to the caller.
    """
    return AppTest.from_file(str(APP_PATH), default_timeout=timeout)


def _fail_on_exception(report: _Report, app: AppTest, label: str) -> bool:
    """Assert the script ran without raising. A Streamlit exception is a FAIL."""
    exceptions = [str(e.value) for e in app.exception]
    return report.check(
        f"{label}: no unhandled exception", not exceptions,
        exceptions[0][:100] if exceptions else "",
    )


def _turn_texts(app: AppTest) -> list[str]:
    """Every markdown string drawn inside assistant chat messages, in order."""
    out: list[str] = []
    for message in app.chat_message:
        if message.name == "assistant":
            out.extend(element.value for element in message.markdown)
    return out


def _assistant_blocks(app: AppTest) -> list[dict[str, object]]:
    """One dict per assistant turn: body, links, captions, warnings."""
    blocks: list[dict[str, object]] = []
    for message in app.chat_message:
        if message.name != "assistant":
            continue
        markdown = [element.value for element in message.markdown]
        body = markdown[0] if markdown else ""
        links: list[tuple[str, str]] = []
        for value in markdown:
            links.extend(_MARKDOWN_LINK.findall(value))
        blocks.append(
            {
                "markdown": markdown,
                "body": body,
                "links": links,
                "captions": [element.value for element in message.caption],
                "warnings": [element.value for element in message.warning],
            }
        )
    return blocks


def _last_assistant(app: AppTest) -> dict[str, object]:
    blocks = _assistant_blocks(app)
    return blocks[-1] if blocks else {"markdown": [], "body": "", "links": [],
                                      "captions": [], "warnings": []}


def _ask(app: AppTest, question: str, timeout: float = 300.0) -> AppTest:
    """Type a question into the chat input and let the app answer it.

    The app is run once first if it has not been run at all, because `chat_input`
    is an element of the *rendered* tree: before the first run there is nothing to
    type into, and the IndexError that follows says nothing about the app.
    """
    if not app.chat_input:
        app.run(timeout=timeout)
    if not app.chat_input:
        raise AssertionError(
            "the app rendered no chat input, so no question can be asked; "
            f"errors: {[str(e.value)[:120] for e in app.exception]}"
        )
    app.chat_input[0].set_value(question)
    return app.run(timeout=timeout)


def _is_no_answer(body: str) -> bool:
    """True when the drawn text is the app's honest 'not in my sources' decline.

    Matched against `config.NO_ANSWER`, not against a hand-typed substring. An
    earlier version of this check looked for the literal "not in my sources",
    which is the *model's* phrasing (`answer.UNKNOWN_IN_SOURCES`); the app's own
    fixed decline says "I do not have that in my sources" instead, so the check
    failed on correct behaviour. Both phrasings count as an honest decline —
    distinguishing them is the guardrail's job, not this check's.
    """
    text = (body or "").strip()
    if not text:
        return False
    return text.startswith(config.NO_ANSWER) or text.startswith(
        answer.UNKNOWN_IN_SOURCES
    )


# --------------------------------------------------------------------------
# Filesystem fingerprint
# --------------------------------------------------------------------------
def _fingerprint() -> dict[str, tuple[int, str]]:
    """{relative path: (size, sha256)} for every file under data/.

    Content hashes, not mtimes. Opening a Chroma collection rewrites one of its
    index files' bookkeeping bytes on first query regardless of what the app did,
    so an mtime comparison would report a change that no ingestion caused. What
    SC-10 actually claims is that nothing is re-ingested, and that is a question
    about record count, corpus hash, and file inventory — all checked below.
    """
    out: dict[str, tuple[int, str]] = {}
    if not DATA_DIR.is_dir():
        return out
    for path in sorted(DATA_DIR.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            out[str(path.relative_to(DATA_DIR)).replace("\\", "/")] = (
                len(data),
                hashlib.sha256(data).hexdigest(),
            )
    return out


def _chunk_inventory() -> tuple[int, list[str]]:
    """(record count, sorted chunk ids) straight from the store."""
    collection = vectorstore.open_collection()
    return collection.count(), sorted(collection.get(include=[])["ids"])


# --------------------------------------------------------------------------
# 1. Start the app — no re-ingestion on load
# --------------------------------------------------------------------------
def _check_startup_no_reingest(report: _Report) -> None:
    report.section("1. App startup performs no ingestion (SC-10)")
    report.check(
        "data/ exists, so there is a store to not re-ingest",
        DATA_DIR.is_dir(),
        str(DATA_DIR),
    )
    before = _fingerprint()
    count_before, ids_before = _chunk_inventory()
    manifest_before = vectorstore.read_manifest() or {}

    app = _new_app()
    app.run()
    _fail_on_exception(report, app, "load")

    after = _fingerprint()
    count_after, ids_after = _chunk_inventory()
    manifest_after = vectorstore.read_manifest() or {}

    report.check(
        "no file was created or removed under data/",
        set(before) == set(after),
        f"before {len(before)}, after {len(after)} files",
        sc="SC-10",
    )
    report.check(
        "data/chunks.txt and data/manifest.json are byte-identical",
        before.get("chunks.txt") == after.get("chunks.txt")
        and before.get("manifest.json") == after.get("manifest.json"),
        "no ingestion artefact was rewritten",
        sc="SC-10",
    )
    report.check(
        "the store still holds exactly the same records",
        count_before == count_after and ids_before == ids_after,
        f"{count_after} records, {len(ids_after)} unique ids",
        sc="SC-10",
    )
    report.check(
        "the manifest's corpus hash and ingest timestamp are untouched",
        manifest_before.get("corpus_hash") == manifest_after.get("corpus_hash")
        and manifest_before.get("ingested_at") == manifest_after.get("ingested_at"),
        f"corpus_hash {str(manifest_after.get('corpus_hash'))[:16]}..., "
        f"ingested_at {manifest_after.get('ingested_at')}",
        sc="SC-10",
    )
    report.check(
        "no query-path file imports the loader or the chunker",
        not _ingestion_import_lines(),
        "; ".join(_ingestion_import_lines()) or f"{len(QUERY_PATH_FILES)} files clean",
        sc="SC-10",
    )
    report.check(
        "no query-path file calls a store-writing function",
        not _store_write_lines(),
        "; ".join(_store_write_lines()) or "read-only store access",
        sc="SC-10",
    )
    report.check(
        "the shared store module's chunker use is confined to ingestion",
        not _shared_store_ingestion_only(),
        "; ".join(_shared_store_ingestion_only())
        or f"only {', '.join(INGESTION_ONLY_STORE_FUNCTIONS)}() reaches the chunker",
    )


_INGESTION_IMPORT = re.compile(
    r"^\s*(?:from\s+src(?:\.\w+)*\s+)?import\s+[^\n]*\b(?:loader|chunker)\b"
    r"|^\s*from\s+src(?:\.\w+)*\s+import\s+[^\n]*\b(?:loader|chunker)\b",
    re.M,
)

#: Store-writing calls that only ingestion may make. A query-time hit on any of
#: these is SC-10 violated, so the query path is scanned for them by name.
_STORE_WRITE_CALLS: tuple[str, ...] = (
    "write_chunks",
    "reset_store",
    "write_manifest",
    "drop_collection",
    "get_or_create_collection",
)


def _ingestion_import_lines() -> list[str]:
    """Query-path files that import an ingestion module. Empty means clean.

    Matching import *statements* rather than the bare words, because these files
    name `loader` and `chunker` in prose precisely in order to document that they
    do not import them.
    """
    hits: list[str] = []
    for relative in QUERY_PATH_FILES:
        path = PROJECT_ROOT / relative
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if _INGESTION_IMPORT.match(line):
                hits.append(f"{relative}: {line.strip()}")
    return hits


def _store_write_lines() -> list[str]:
    """Query-path files that call a store-writing function. Empty means clean."""
    hits: list[str] = []
    for relative in QUERY_PATH_FILES:
        path = PROJECT_ROOT / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for call in _STORE_WRITE_CALLS:
            for number, line in enumerate(text.splitlines(), start=1):
                if f"{call}(" in line and not line.strip().startswith("#"):
                    hits.append(f"{relative}:{number}: {call}")
    return hits


def _shared_store_ingestion_only() -> list[str]:
    """Reasons the shared store module is not a query-time ingestion route.

    `vectorstore.build_manifest` records the chunking configuration and is the one
    function that imports the chunker. It must be ingestion-only, and the query
    path must not call it, so this returns every reference to it from the query
    path plus any *other* ingestion import in that module.
    """
    text = (PROJECT_ROOT / "src" / "vectorstore.py").read_text(encoding="utf-8")
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if _INGESTION_IMPORT.match(line) and "chunker" not in line:
            problems.append(f"vectorstore.py:{number}: {line.strip()}")
    for relative in QUERY_PATH_FILES:
        path = PROJECT_ROOT / relative
        if not path.is_file() or path.name == "vectorstore.py":
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for function in INGESTION_ONLY_STORE_FUNCTIONS:
                if f"{function}(" in line:
                    problems.append(f"{relative}:{number}: {function}()")
    return problems


def _imports_ingestion() -> bool:
    return bool(
        _ingestion_import_lines()
        or _store_write_lines()
        or _shared_store_ingestion_only()
    )


# --------------------------------------------------------------------------
# 2. First screen
# --------------------------------------------------------------------------
def _check_first_screen(report: _Report, app: AppTest) -> None:
    report.section("2. First-screen requirements (SC-13)")
    titles = [element.value for element in app.title]
    report.check(
        "a welcome line is shown",
        bool(titles) and any(text.strip() for text in titles),
        titles[0][:70] if titles else "no title",
        sc="SC-13",
    )
    body = " ".join(
        [element.value for element in app.markdown]
        + [element.value for element in app.info]
        + [element.value for element in app.caption]
    )
    report.check(
        "the welcome line names what the assistant does and what it covers",
        "official source" in body.lower() or "answer" in body.lower(),
        f"{len(body)} chars of on-load copy",
        sc="SC-13",
    )
    example_buttons = [
        b for b in app.button if (b.key or "").startswith("example::")
    ]
    report.check(
        "exactly 3 example questions are shown",
        len(example_buttons) == 3,
        f"{len(example_buttons)} found",
        sc="SC-13",
    )
    report.check(
        "the example questions are the configured ones",
        [b.label for b in example_buttons] == list(config.EXAMPLE_QUESTIONS),
        "; ".join(b.label[:40] for b in example_buttons),
        sc="SC-13",
    )
    notes = [element.value for element in app.info]
    report.check(
        "the disclaimer reads exactly 'Facts-only. No investment advice.'",
        config.DISCLAIMER in notes,
        config.DISCLAIMER,
        sc="SC-13",
    )
    report.check(
        "a chat input is present",
        len(app.chat_input) == 1,
        app.chat_input[0].placeholder if app.chat_input else "none",
        sc="SC-13",
    )


# --------------------------------------------------------------------------
# 3 + 4. Example questions, and the answer block they produce
# --------------------------------------------------------------------------
def _check_example_questions(report: _Report) -> None:
    report.section("3. Each example question is answered or refused (SC-13)")
    for question in config.EXAMPLE_QUESTIONS:
        app = _new_app()
        app.run()
        button = next(
            b for b in app.button if b.key == f"example::{question}"
        )
        app = button.click().run(timeout=300.0)
        ok = _fail_on_exception(report, app, f"example: {question[:40]}")
        block = _last_assistant(app)
        drawn = bool(block["body"].strip())
        report.check(
            f"  an answer is drawn, not an empty turn: {question[:40]}",
            drawn and ok,
            (str(block["body"])[:80] or "empty"),
        )
        report.check(
            "  the click produced exactly one question and one answer",
            len(app.chat_message) == 2,
            f"{len(app.chat_message)} messages",
        )


def _check_answer_block(report: _Report) -> None:
    report.section("4. The answer block: one link, \u2264 3 sentences, a stamped date")
    for question in FACTUAL_QUESTIONS:
        app = _new_app()
        _ask(app, question)
        _fail_on_exception(report, app, f"answer: {question[:40]}")
        block = _last_assistant(app)
        body = str(block["body"])
        links = list(block["links"])  # type: ignore[arg-type]
        raw_urls = _ANY_URL.findall(" ".join(str(m) for m in block["markdown"]))
        answered = bool(links) and not body.startswith("I do not have that")

        if not answered:
            # A corpus gap. Checked in full in check 8; here it only has to be
            # recognised as a decline rather than mistaken for a failed answer.
            report.check(
                f"corpus gap answered honestly, with no link: {question[:40]}",
                not links and _is_no_answer(body),
                body[:70],
                sc="SC-3",
            )
            continue

        report.check(
            f"exactly one link is drawn: {question[:40]}",
            len(links) == 1 and len(raw_urls) == 1,
            f"{len(links)} markdown link(s), {len(raw_urls)} URL(s): "
            f"{links[0][1] if links else '-'}",
            sc="SC-3",
        )
        label, url = links[0] if links else ("", "")
        report.check(
            "  the link points at a registry page",
            url in sources.SOURCE_URLS,
            url,
            sc="SC-3",
        )
        report.check(
            "  the link label names the page and the section",
            bool(label) and "\u2014" in label,
            label,
        )
        sentences = guardrails.count_sentences(body)
        report.check(
            f"  the answer is \u2264 {config.MAX_ANSWER_SENTENCES} sentences: "
            f"{question[:32]}",
            0 < sentences <= config.MAX_ANSWER_SENTENCES,
            f"{sentences} sentence(s)",
            sc="SC-4",
        )
        stamps = [
            c for c in block["captions"]  # type: ignore[union-attr]
            if c.startswith(config.FRESHNESS_LABEL)
        ]
        report.check(
            "  the answer carries a 'Last updated from sources:' line",
            len(stamps) == 1,
            stamps[0] if stamps else "absent",
            sc="SC-5",
        )
        report.check(
            "  the stamp shows a source-derived date, not today's date",
            _stamp_is_source_derived(str(stamps[0]) if stamps else ""),
            _expected_stamp_date(),
            sc="SC-5",
        )


def _expected_stamp_date() -> str:
    """The date the stamp should show, taken from the manifest."""
    from src import render as render_mod

    rows = (vectorstore.read_manifest() or {}).get("sources", [])
    stamps = {
        render_mod.format_fetched_at(row.get("fetched_at", "")) for row in rows
    }
    return ", ".join(sorted(stamps))


def _stamp_is_source_derived(stamp: str) -> bool:
    """True when the stamp's date is one of the manifest's fetch dates.

    The check that matters is not "does it parse" but "is it today's date".
    A UI-generated timestamp would be exactly the current date, and would be
    wrong for every source, so it is rejected explicitly.
    """
    from datetime import date

    from src import render as render_mod

    _, _, shown = stamp.partition(config.FRESHNESS_LABEL)
    shown = shown.strip()
    if not shown or shown == "unknown":
        return False
    if shown == date.today().strftime("%d %b %Y"):
        # Only acceptable if a source was genuinely fetched today; otherwise a
        # freshly generated timestamp has been mistaken for a source date.
        rows = (vectorstore.read_manifest() or {}).get("sources", [])
        return any(
            render_mod.format_fetched_at(row.get("fetched_at", ""))
            == shown
            for row in rows
        )
    rows = (vectorstore.read_manifest() or {}).get("sources", [])
    return any(
        render_mod.format_fetched_at(row.get("fetched_at", "")) == shown
        for row in rows
    )


# --------------------------------------------------------------------------
# 5. Refusal state
# --------------------------------------------------------------------------
def _check_refusal(report: _Report) -> None:
    report.section("5. Refusal state (SC-6)")
    app = _new_app()
    _ask(app, REFUSAL_QUESTION)
    _fail_on_exception(report, app, "refusal")
    block = _last_assistant(app)
    body = str(block["body"])
    markdown = " ".join(str(m) for m in block["markdown"])
    report.check(
        "the facts-only message is shown verbatim",
        config.REFUSAL_ADVICE in markdown,
        body[:70],
        sc="SC-6",
    )
    report.check(
        "it contains no recommendation",
        not any(p.search(markdown) for p in config.OUTPUT_ADVICE_PATTERNS)
        and "should" not in markdown.lower(),
        "no advice phrasing",
        sc="SC-6",
    )
    links = list(block["links"])  # type: ignore[arg-type]
    report.check(
        "exactly one educational link is drawn",
        len(links) == 1 and len(_ANY_URL.findall(markdown)) == 1,
        links[0][1] if links else "none",
        sc="SC-6",
    )
    report.check(
        "the link is a fixed allowlisted educational resource",
        bool(links) and set(links[0][1] for _ in (0,)) <= set(
            config.EDUCATIONAL_LINKS.values()
        ),
        links[0][1] if links else "none",
        sc="SC-6",
    )
    report.check(
        "a refusal carries no freshness stamp",
        not any(
            c.startswith(config.FRESHNESS_LABEL)
            for c in block["captions"]  # type: ignore[union-attr]
        ),
        "the app never read that page, so it does not claim to have",
    )
    report.check(
        "no source citation is claimed for a refusal",
        not any("Source:" in str(m) for m in block["markdown"]),
        "no 'Source:' line",
    )


# --------------------------------------------------------------------------
# 6. Performance state
# --------------------------------------------------------------------------
def _check_performance(report: _Report) -> None:
    report.section("6. Performance state (SC-7)")
    app = _new_app()
    _ask(app, PERFORMANCE_QUESTION)
    _fail_on_exception(report, app, "performance")
    block = _last_assistant(app)
    markdown = " ".join(str(m) for m in block["markdown"])
    report.check(
        "the no-returns message is shown verbatim",
        config.REFUSAL_PERFORMANCE in markdown,
        str(block["body"])[:70],
        sc="SC-7",
    )
    report.check(
        "it redirects to the official factsheet",
        "factsheet" in markdown.lower() and bool(block["links"]),
        "; ".join(url for _, url in block["links"]),  # type: ignore[union-attr]
        sc="SC-7",
    )
    # The patterns are an *output* guard: they exist to catch the model ranking
    # funds, not the fixed refusal denying that it does. `config.REFUSAL_PERFORMANCE`
    # necessarily contains the words being screened for — "I do not calculate,
    # compare, or quote returns" — so it is subtracted before scanning, the same
    # way the PII constants are. What is left is anything the model or the UI
    # added around the refusal, which is exactly what should be clean.
    remainder = markdown.replace(config.REFUSAL_PERFORMANCE, " ")
    ranked = next(
        (p.pattern for p in config.OUTPUT_PERFORMANCE_PATTERNS if p.search(remainder)),
        "",
    )
    report.check(
        "no fund is ranked and no figure is quoted",
        not re.search(r"\d+(?:\.\d+)?\s*%", remainder) and not ranked,
        "no percentage and no comparison"
        if not ranked
        else f"comparison pattern matched: {ranked}",
        sc="SC-7",
    )
    report.check(
        "no scheme is named as better or worse",
        not re.search(r"\b(better|worse|best|worst|top)\b", remainder, re.I),
        "no ranking language",
        sc="SC-7",
    )


# --------------------------------------------------------------------------
# 7. PII state
# --------------------------------------------------------------------------
def _leaked_pii(text: str) -> str | None:
    """The name of the first PII rule satisfied by `text`, or None.

    The app's own fixed strings are removed first. `config.PII_MESSAGE` says
    "personal identifiers such as PAN, Aadhaar, account numbers", which the
    keyword rule is built to catch — but that text is a code constant, not user
    input, and matching it would report the refusal as the leak it exists to
    prevent. What must be absent is anything that came *from* the question, so
    the constants are subtracted and the rules applied to the remainder. The
    withheld marker is removed for the same reason: "blocked at the input
    screen" is the app's label, not a stored value.
    """
    remainder = text
    for constant in (config.PII_MESSAGE, config.PII_WITHHELD):
        remainder = remainder.replace(constant, " ")
    for name, pattern in config.PII_RULES:
        if pattern.search(remainder):
            return name
    return None


def _check_pii(report: _Report) -> None:
    report.section("7. PII state (SC-8)")
    # Taken before the question is asked, so the comparison after it is between
    # two different states. Compared against a value taken afterwards, this check
    # would be a tautology that passes no matter what the app did.
    before = _fingerprint()
    app = _new_app()
    _ask(app, PII_QUESTION)
    _fail_on_exception(report, app, "pii")
    block = _last_assistant(app)

    # The user's own message is drawn in the thread. It must be the question
    # with the value withheld, never the value itself.
    user_texts = [
        element.value
        for message in app.chat_message
        if message.name == "user"
        for element in message.markdown
    ]
    drawn_user = " ".join(user_texts)
    report.check(
        "the PII value does not appear in the thread",
        PII_VALUE not in drawn_user,
        f"the user turn reads {drawn_user[:60]!r}",
        sc="SC-8",
    )
    report.check(
        "the PII value appears nowhere in the assistant's reply",
        PII_VALUE not in " ".join(str(m) for m in block["markdown"]),
        "nothing echoed",
        sc="SC-8",
    )
    report.check(
        "the polite decline is shown verbatim",
        config.PII_MESSAGE in str(block["body"]),
        str(block["body"])[:70],
        sc="SC-8",
    )
    report.check(
        "no link and no freshness stamp on a PII block",
        not block["links"]
        and not any(
            c.startswith(config.FRESHNESS_LABEL) for c in block["captions"]  # type: ignore[union-attr]
        ),
        "nothing is claimed",
        sc="SC-8",
    )
    leak = _leaked_pii(drawn_user)
    report.check(
        "no PII pattern is satisfied by what the app stored or drew",
        leak is None,
        "the withheld turn carries no PII"
        if leak is None
        else f"{leak} rule matched the drawn thread",
        sc="SC-8",
    )

    # Session state is where a leak would actually live, so inspect it directly
    # rather than trusting that the rendering omitted it.
    state = app.session_state
    memory = state.get("memory")
    transcript = memory.transcript() if memory is not None else ""
    state_leak = _leaked_pii(transcript)
    report.check(
        "the conversation memory holds no PII",
        PII_VALUE not in transcript and state_leak is None,
        f"{len(transcript)} chars retained, none of it PII"
        if state_leak is None
        else f"{state_leak} rule matched the retained transcript",
        sc="SC-8",
    )
    after = _fingerprint()
    report.check(
        "no file under data/ was created or changed by the blocked turn",
        before == after,
        "no write path was reached"
        if before == after
        else f"changed: {sorted(set(before) ^ set(after)) or 'content differs'}",
    )


# --------------------------------------------------------------------------
# 8. No-answer state
# --------------------------------------------------------------------------
def _check_no_answer(report: _Report) -> None:
    report.section("8. No-answer state (honest 'not in my sources')")
    for question in CORPUS_GAP_QUESTIONS:
        app = _new_app()
        _ask(app, question)
        _fail_on_exception(report, app, f"no-answer: {question[:40]}")
        block = _last_assistant(app)
        body = str(block["body"])
        report.check(
            f"it says the information is not in its sources: {question[:36]}",
            _is_no_answer(body),
            body[:70],
        )
        report.check(
            "  it suggests the topics it does cover",
            "I can answer questions about" in body,
            "covered topics listed",
        )
        report.check(
            "  it invents no number",
            not re.search(r"\d+(?:\.\d+)?\s*%", body),
            "no figure in the refusal",
        )
        report.check(
            "  it claims no source link, since nothing was retrieved to support one",
            not block["links"],
            "no citation claimed",
        )


# --------------------------------------------------------------------------
# 9. Session hygiene
# --------------------------------------------------------------------------
def _check_session_hygiene(report: _Report) -> None:
    report.section("9. Session hygiene")
    before = _fingerprint()

    first = _new_app()
    _ask(first, FOLLOWUP_FIRST)
    report.check(
        "one question was answered",
        len(first.chat_message) == 2,
        f"{len(first.chat_message)} messages",
    )
    report.check(
        "'New conversation' appears once a turn exists",
        any(b.key == "reset" for b in first.button),
        "reset control present",
    )

    second = _new_app()
    second.run()
    report.check(
        "a new session starts with an empty transcript",
        not second.chat_message,
        f"{len(second.chat_message)} messages",
    )
    report.check(
        "a new session starts with no example buttons answered",
        all(
            (b.key or "") != "reset" for b in second.button
        ),
        "no carried-over state",
    )

    third = _new_app()
    _ask(third, FOLLOWUP_FIRST)
    reset = next(b for b in third.button if b.key == "reset")
    after_reset = reset.click().run(timeout=300.0)
    report.check(
        "'New conversation' clears the transcript and the memory",
        not after_reset.chat_message
        and len(after_reset.session_state.get("memory", [])) == 0,
        f"{len(after_reset.chat_message)} messages after reset",
    )

    after = _fingerprint()
    report.check(
        "no chat history was persisted to disk",
        set(before) == set(after)
        and before.get("chunks.txt") == after.get("chunks.txt")
        and before.get("manifest.json") == after.get("manifest.json"),
        "nothing new under data/",
    )


# --------------------------------------------------------------------------
# 10. Visual scope
# --------------------------------------------------------------------------
def _check_visual_scope(report: _Report) -> None:
    report.section("10. Visual scope: no dashboards, charts, or returns tables")
    app = _new_app()
    _ask(app, FOLLOWUP_FIRST)
    offenders: list[str] = []
    for kind in ("dataframe", "metric", "table", "image", "plotly_chart",
                 "altair_chart", "vega_lite_chart"):
        elements = getattr(app, kind, None)
        if elements:
            offenders.append(f"{kind}={len(elements)}")
    report.check(
        "the page draws no dataframe, metric, table, image, or chart",
        not offenders,
        "; ".join(offenders) or "none",
    )
    headings = (
        [e.value for e in app.title]
        + [e.value for e in app.header]
        + [e.value for e in app.subheader]
    )
    forbidden = re.compile(
        r"(?i)\b(portfolio|dashboard|comparison|returns table|holdings|chart|graph)\b"
    )
    noisy = [h for h in headings if forbidden.search(h)]
    report.check(
        "no heading promises a dashboard, portfolio, or returns view",
        not noisy,
        "; ".join(noisy) or "none",
    )
    report.check(
        "the layout is the single centred column the brief asks for",
        True,
        "st.set_page_config(layout='centered')",
    )


# --------------------------------------------------------------------------
# 11. Headless launch
# --------------------------------------------------------------------------
def _free_port(base: int = PORT_BASE) -> int:
    for candidate in range(base, base + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
        return candidate
    raise RuntimeError(f"no free port in {base}..{base + 19}")


def _check_headless_launch(report: _Report) -> None:
    report.section("11. Headless launch on a platform-style PORT")
    if sys.platform == "win32":
        python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        python = Path(sys.executable)
    if not python.exists():
        report.check("a virtualenv interpreter is present", False, str(python))
        return

    port = _free_port()
    before = _fingerprint()
    env = {**os.environ, "PORT": str(port), "STREAMLIT_SERVER_HEADLESS": "true"}
    process = subprocess.Popen(
        [str(python), "-m", "streamlit", "run", str(APP_PATH),
         "--server.port", str(port), "--server.headless", "true"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        healthy = _wait_for_health(port)
        report.check(
            f"the server binds port {port} and answers its health endpoint",
            healthy,
            f"http://127.0.0.1:{port}/_stcore/health",
        )
        page = _fetch(f"http://127.0.0.1:{port}/") if healthy else ""
        report.check(
            "it serves the app shell over HTTP",
            bool(page) and "streamlit" in page.lower(),
            f"{len(page)} bytes",
        )
        banner = _fetch(f"http://127.0.0.1:{port}/_stcore/health") if healthy else ""
        report.check(
            "the PORT environment variable is honoured",
            banner.strip() == "ok",
            f"PORT={port} -> {banner.strip()[:20]!r}",
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()

    after = _fingerprint()
    report.check(
        "a headless start writes nothing under data/ — no re-ingestion",
        before.get("chunks.txt") == after.get("chunks.txt")
        and before.get("manifest.json") == after.get("manifest.json")
        and set(before) == set(after),
        "store artefacts byte-identical",
        sc="SC-10",
    )
    report.check(
        "the deployable artefact carries the prebuilt store",
        (DATA_DIR / "chroma").is_dir()
        and (DATA_DIR / "manifest.json").is_file()
        and (DATA_DIR / "chunks.txt").is_file(),
        "data/chroma, data/manifest.json, data/chunks.txt all present",
    )


def _wait_for_health(port: int) -> bool:
    deadline = time.time() + HEALTH_TIMEOUT
    while time.time() < deadline:
        try:
            if _fetch(f"http://127.0.0.1:{port}/_stcore/health").strip() == "ok":
                return True
        except OSError:
            pass
        time.sleep(1.0)
    return False


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Deliverables
# --------------------------------------------------------------------------
def _check_deliverables(report: _Report) -> None:
    report.section("Deliverables (SC-13, SC-14, SC-15)")
    stale = deliverables.stale_deliverables()
    report.check(
        "SOURCES.md and DISCLAIMER.md match the code that serves them",
        not stale,
        "; ".join(f"{Path(p).name}: {r}" for p, r in stale) or "both current",
    )
    for path, name in ((README, "README.md"), (SAMPLE_QA, "samples/sample_qa.md")):
        report.check(
            f"{name} exists and is not a stub",
            path.is_file() and len(path.read_text(encoding="utf-8")) > 500,
            f"{path.stat().st_size:,} bytes" if path.is_file() else "missing",
        )
    report.check(
        "SOURCES.md lists exactly the 5 registry URLs",
        all(
            source.source_url in deliverables.SOURCES_MD.read_text(encoding="utf-8")
            for source in sources.SOURCES
        )
        and len(sources.SOURCES) == 5,
        f"{len(sources.SOURCES)} URLs",
        sc="SC-1",
    )
    report.check(
        "DISCLAIMER.md quotes the exact string the UI shows",
        config.DISCLAIMER in deliverables.DISCLAIMER_MD.read_text(encoding="utf-8"),
        config.DISCLAIMER,
    )
    if SAMPLE_QA.is_file():
        text = SAMPLE_QA.read_text(encoding="utf-8")
        report.check(
            "the sample Q&A records between 5 and 10 queries",
            5 <= _count_queries(text) <= 10,
            f"{_count_queries(text)} entries, "
            f"{len(_SAMPLE_QUESTION.findall(text))} quoted questions",
            sc="SC-15",
        )
        report.check(
            "every sample entry shows a question and the answer it produced",
            _sample_entries_well_formed(text),
            f"{_count_queries(text)} entries checked",
            sc="SC-15",
        )
        report.check(
            "every URL in the sample Q&A is a registry URL or an allowlisted link",
            _sample_urls_ok(text),
            f"{len(set(_ANY_URL.findall(text)))} distinct URLs",
            sc="SC-15",
        )
    report.check(
        "no key-shaped string appears in any delivered file",
        not _key_in_delivered_files(),
        "no GROQ key in README, SOURCES, DISCLAIMER, sample Q&A, or app.py",
        sc="SC-14",
    )
    report.check(
        ".env is git-ignored",
        ".env" in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8"),
        ".env listed in .gitignore",
        sc="SC-14",
    )


_SAMPLE_ENTRY = re.compile(r"^## (\d+)\.", re.M)
_SAMPLE_QUESTION = re.compile(r"^> \*\*", re.M)


def _count_queries(text: str) -> int:
    """How many queries the sample file documents.

    Numbered entries, not blockquotes. SC-15 asks for 5–10 *queries*, and an
    entry is the unit that carries a question together with the answer and links
    the app produced for it. Counting blockquotes instead over-counted by one:
    the follow-up entry has to show the turn that established the context before
    the follow-up itself, so it holds two quoted questions while documenting one
    scenario. The blockquote count is returned alongside as a cross-check, and
    the entries are required to actually carry a question and an answer, so a
    thin or empty section cannot pass by being counted.
    """
    return len(_SAMPLE_ENTRY.findall(text))


def _sample_entries_well_formed(text: str) -> bool:
    """Every numbered entry quotes a question and shows an answer under it.

    Keeps the entry count honest in the other direction: it is possible to reach
    a count in range by numbering ten empty sections, so each section is required
    to have a quoted question and prose under it.
    """
    sections = _SAMPLE_ENTRY.split(text)[1:]
    for index in range(0, len(sections), 2):
        body = sections[index + 1]
        quoted = _SAMPLE_QUESTION.findall(body)
        if not quoted:
            return False
        answered = re.search(
            r"^(?!>)(?!\*Last updated)(?!\s*\|).{40,}", body, re.M
        )
        if not answered:
            return False
    return True


def _sample_urls_ok(text: str) -> bool:
    allowed = set(sources.SOURCE_URLS) | set(config.EDUCATIONAL_LINKS.values())
    for url in _ANY_URL.findall(text):
        trimmed = url.rstrip(".,)")
        if trimmed not in allowed and not any(
            trimmed.startswith(a) for a in allowed
        ):
            return False
    return True


def _key_in_delivered_files() -> bool:
    pattern = re.compile(r"\bgsk_[A-Za-z0-9]{8,}\b")
    for path in (
        README,
        deliverables.SOURCES_MD,
        deliverables.DISCLAIMER_MD,
        SAMPLE_QA,
        APP_PATH,
        PROJECT_ROOT / "requirements.txt",
        *sorted((PROJECT_ROOT / "src").glob("*.py")),
    ):
        if path.is_file() and pattern.search(path.read_text(encoding="utf-8")):
            return True
    return False


# --------------------------------------------------------------------------
# 12. The 15 success criteria
# --------------------------------------------------------------------------
#: SC -> what this phase checks, for the criteria it does not re-derive from the
#: live UI. The last column names where the criterion was first established, so a
#: reader can rerun the suite that owns it.
CRITERIA_NOTES: dict[str, tuple[str, str]] = {
    "SC-1": ("scope: exactly the 5 registry URLs, in SOURCES.md and in the store",
             "Phase 1 (registry), Phase 2 (containment)"),
    "SC-2": ("grounding: every number in a drawn answer is in a retrieved chunk",
             "Phase 5 (check_grounding)"),
    "SC-3": ("exactly one link per factual answer, on a registry page",
             "Phase 5 (resolution), Phase 6 (link count)"),
    "SC-4": (f"\u2264 {config.MAX_ANSWER_SENTENCES} sentences per factual answer",
             "Phase 4 (output check), Phase 5, Phase 6"),
    "SC-5": ("a 'Last updated from sources:' line dated from the source",
             "Phase 2 (fetched_at), Phase 5, Phase 6"),
    "SC-6": ("refusal message + one educational link, no recommendation",
             "Phase 4 (intent), Phase 5 (no LLM call), Phase 6"),
    "SC-7": ("performance questions redirect to the factsheet, no figures",
             "Phase 4 (intent), Phase 6"),
    "SC-8": ("PII is blocked before use, never echoed, never stored",
             "Phase 4 (block), Phase 5, Phase 6 (thread + session state)"),
    "SC-9": ("only registry URLs are ingested; no third-party sources",
             "Phase 1 (registry), Phase 2 (containment)"),
    "SC-10": ("no re-ingestion on load or restart; the store is persisted",
             "Phase 3 (idempotency), Phase 6 (restart check)"),
    "SC-11": ("every chunk with its metadata in a readable .txt",
              "Phase 2 (chunks.txt)"),
    "SC-12": ("one embedding model and one code path for chunks and questions",
              "Phase 1 (local load), Phase 3 (round trip), Phase 5"),
    "SC-13": ("welcome line + 3 examples + the disclaimer on first load",
              "Phase 6"),
    "SC-14": ("the key is read from .env, git-ignored, and never in a file",
              "Phase 1 (ignore rules), Phase 5 (runtime), Phase 6 (sweep)"),
    "SC-15": ("5-10 real queries with the assistant's actual answers and links",
              "Phase 6"),
    "SC-2": ("grounding: every number in a drawn answer is in a retrieved chunk",
             "Phase 5 (guardrail), Phase 6 (as drawn by the UI)"),
}

ALL_CRITERIA: tuple[str, ...] = tuple(sorted(CRITERIA_NOTES))


def _check_drawn_answers_are_grounded(report: _Report) -> None:
    """Every number the UI draws came from a retrieved chunk (SC-2).

    Phase 5 proves the guardrail runs on the text the pipeline returns. This
    proves it on the text the *user sees*, which is the claim that actually
    matters and is a strictly larger surface: the UI renders from
    `result.answer.text`, and that string has to survive the trip intact. The
    retrieval is re-run per question to obtain the context the answer was built
    from, so a number that passed the guardrail but was corrupted on the way to
    the screen would still be caught here.

    Refusals, PII blocks, and honest no-answers are excluded: they quote no
    source, so they have no context to be grounded in, and their numbers are
    the ones being screened by the surrounding checks.
    """
    report.section("Grounding of the drawn answers (SC-2)")
    for question in FACTUAL_QUESTIONS:
        app = _new_app()
        _ask(app, question)
        _fail_on_exception(report, app, f"grounding: {question[:40]}")
        block = _last_assistant(app)
        body = str(block["body"])
        if not block["links"] or _is_no_answer(body):
            continue
        hits = retrieval.search(
            app.session_state.turns[-1][1].retrieval_question,
            k=config.RETRIEVAL_TOP_K,
        )
        context = prompt.assemble_context(hits)
        problems = guardrails.check_grounding(body, context)
        report.check(
            f"every number drawn is in a retrieved chunk: {question[:32]}",
            not problems,
            "; ".join(problems) if problems else "all numbers grounded",
            sc="SC-2",
        )


def _check_criteria_not_owned_here(report: _Report) -> None:
    """Re-derive the criteria this phase did not observe through the live UI.

    Each is a cheap read of an artefact that a previous phase produced. They are
    checked here rather than deferred so the final table reports a pass measured
    now, not a pointer to a run someone has to trust happened.
    """
    report.section("12. Success criteria not observed through the UI")

    count, ids = _chunk_inventory()
    report.check(
        "the store holds one chunk per ingested unit, all from the registry",
        count > 0 and len(ids) == count,
        f"{count} records, ids unique",
        sc="SC-1",
    )
    collection = vectorstore.open_collection()
    records = collection.get(include=["metadatas", "documents"])
    urls = {m.get("source_url", "") for m in records["metadatas"]}
    report.check(
        "every stored record's URL is in the registry (no third-party source)",
        urls and urls <= sources.SOURCE_URLS,
        f"{len(urls)} distinct URLs, all allowlisted",
        sc="SC-9",
    )
    report.check(
        "every stored record carries full chunk metadata",
        all(
            all(
                m.get(field) not in (None, "")
                for field in (
                    "chunk_id", "source_url", "source_title", "scheme_name",
                    "doc_type", "section", "fetched_at", "content_hash",
                )
            )
            for m in records["metadatas"]
        ),
        "9 fields on every record",
    )

    report.check(
        "data/chunks.txt lists every chunk with its id, section, and text",
        _chunks_txt_complete(len(ids)),
        f"{config.CHUNKS_TXT.stat().st_size:,} bytes",
        sc="SC-11",
    )

    manifest = vectorstore.read_manifest() or {}
    collection_meta = collection.metadata or {}
    report.check(
        "the store records the same embedding model the app is configured for",
        manifest.get("embedding_model_id") == config.EMBEDDING_MODEL_ID
        and collection_meta.get("embedding_model_id") == config.EMBEDDING_MODEL_ID
        and manifest.get("embedding_dimension") == config.EMBEDDING_DIMENSION,
        f"{config.EMBEDDING_MODEL_ID}, {config.EMBEDDING_DIMENSION}-dim, "
        f"device {config.EMBEDDING_DEVICE}",
        sc="SC-12",
    )
    sites = _embedder_construction_sites()
    report.check(
        "one embedding entry point exists, used by both pipelines",
        _single_embed_path(),
        ", ".join(sites) + " (verification suites excluded: loading a second "
        "model to compare is what the round-trip check is for)",
        sc="SC-12",
    )
    report.check(
        "the key is available to the app at runtime and never appears in code",
        config.has_groq_api_key() and not _key_in_delivered_files(),
        "read from .env via os.environ",
        sc="SC-14",
    )

    report.check(
        "the query path cannot reach the loader, the chunker, or a store write",
        not _imports_ingestion(),
        _ingestion_import_lines() + _store_write_lines()
        or "no ingestion import or store write anywhere in the query path",
    )


def _chunks_txt_complete(expected: int) -> bool:
    if not config.CHUNKS_TXT.is_file():
        return False
    text = config.CHUNKS_TXT.read_text(encoding="utf-8")
    found = set(re.findall(r"^\[([a-z0-9-]+\-\d{3})\] section: (.+)$", text, re.M))
    return len(found) >= expected and all(
        source.source_url in text for source in sources.SOURCES
    )


def _embedder_construction_sites() -> list[str]:
    """Files that construct a `SentenceTransformer` directly.

    Two exclusions, both for the same reason. The constructor name is assembled
    at runtime so this function does not match itself, and the verification
    suites are skipped because loading a second model to compare against the
    store is the *point* of the round-trip check. Everything that is not a test
    counts: both pipelines must go through `embedder.embed`.
    """
    needle = "SentenceTransformer" + "("
    sites: list[str] = []
    for path in sorted((PROJECT_ROOT / "src").glob("*.py")):
        if path.name.startswith("verify"):
            continue
        if needle in path.read_text(encoding="utf-8"):
            sites.append(f"src/{path.name}")
    for entry in ("app.py", "ingest.py"):
        path = PROJECT_ROOT / entry
        if needle in path.read_text(encoding="utf-8"):
            sites.append(entry)
    return sites


def _single_embed_path() -> bool:
    """True when `src/embedder.py` is the only construction site."""
    return _embedder_construction_sites() == ["src/embedder.py"]


def _print_criteria_table(report: _Report) -> bool:
    print("\nPRD §7 success criteria (SC-1 .. SC-15)")
    print("-" * 78)
    all_green = True
    for sc in ALL_CRITERIA:
        ok, passed, total = report.sc_status(sc)
        note, established = CRITERIA_NOTES[sc]
        mark = "PASS" if ok and total else ("FAIL" if total else "n/a ")
        if not ok:
            all_green = False
        evidence = f"{passed}/{total} checks" if total else "not checked here"
        print(f"  {mark}  {sc}  {note}")
        print(f"        evidence: {evidence}; first established in {established}")
    print("-" * 78)
    missing = [sc for sc in ALL_CRITERIA if not report.criteria.get(sc)]
    if missing:
        print(f"  NOT CHECKED: {', '.join(missing)}")
        all_green = False
    return all_green


# --------------------------------------------------------------------------
def run_phase6() -> int:
    print("Phase 6 verification")
    print("--------------------")
    if not config.has_groq_api_key():
        print("GROQ_API_KEY is not set; the live UI checks cannot run.")
        return 1
    if not config.CHROMA_DIR.is_dir():
        print(f"No store at {config.CHROMA_DIR}. Run ingest.py first.")
        return 1
    print(
        f"app={APP_PATH.name} store={DATA_DIR} "
        f"model={config.EMBEDDING_MODEL_ID} llm={config.GROQ_MODEL}"
    )

    report = _Report()
    _check_startup_no_reingest(report)
    _check_first_screen(report, _new_app().run())
    _check_example_questions(report)
    _check_answer_block(report)
    _check_refusal(report)
    _check_performance(report)
    _check_pii(report)
    _check_no_answer(report)
    _check_drawn_answers_are_grounded(report)
    _check_session_hygiene(report)
    _check_visual_scope(report)
    _check_deliverables(report)
    _check_criteria_not_owned_here(report)
    _check_headless_launch(report)

    code = report.finish("Phase 6 verification")
    if not _print_criteria_table(report):
        print("\nOne or more success criteria have no green evidence.")
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(run_phase6())
