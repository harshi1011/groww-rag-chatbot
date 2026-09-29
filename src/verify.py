"""Phase 2 verification — the 11 checks from doc/implementation.md.

Run with:

    .venv\\Scripts\\python.exe ingest.py --verify-only

Every check prints PASS or FAIL with the observed value, and the process exits
non-zero if any check fails, so this is usable as a gate.

The checks run against the live sources, so they also prove the pages are still
reachable and still have the shape the normaliser expects.
"""

from __future__ import annotations

import re

from src import chunker, config, loader
from src.config import PROJECT_ROOT
from src.sources import SOURCES, is_allowed_url

#: Words that only ever appear in navigation, footer, or cookie chrome.
BOILERPLATE = (
    "cookie",
    "sign in",
    "log in",
    "sign up",
    "download app",
    "all rights reserved",
    "terms of use",
    "privacy policy",
    "disclaimer",
    "groww asset management",
    "amc:",
)

#: Out-of-scope fields that must never reach a chunk.
FORBIDDEN_FIELDS = (
    (r"\bNAV\b", "live NAV quote"),
    (r"(?i)^Rating", "distributor star rating"),
    (r"Rank \(total assets\)", "category rank"),
    (r"Category average", "category performance"),
    (r"(?i)3Y annualised", "returns ticker"),
)

#: Scheme names that appear on the pages but are not in the corpus. Seeing one
#: means the manager-card or compare-table blocks were not removed.
OUT_OF_SCOPE_SCHEMES = (
    "HDFC Value Fund",
    "HDFC Floating Interest",
    "HDFC Credit Risk",
    "HDFC Banking and PSU",
    "HDFC Arbitrage",
    "HDFC Large & Mid",
    "HDFC Medium Term",
    "HDFC Short Term",
    "HDFC Ultra Short",
    "HDFC Conservative Hybrid",
    "HDFC Corporate Bond",
    "HDFC Infrastructure",
    "HDFC Focused",
    "HDFC Retirement",
    "HDFC Equity Savings",
    "HDFC Liquid",
    "HDFC Multi Asset",
    "HDFC Income Plus",
    "HDFC Children's",
    "HDFC NIFTY 50 ETF",
    "HDFC BSE Sensex",
    "HDFC Silver",
    "HDFC Gold",
    "HDFC Developed World",
    "Invesco India",
    "ICICI Prudential",
    "Nippon India",
    "Bandhan",
    "DSP Dynamic",
    "Aditya Birla",
)

#: Fact types the brief names, and the pattern that proves each is present.
REQUIRED_FACTS = (
    ("expense ratio", r"(?i)expense ratio"),
    ("minimum SIP", r"(?i)min\. for sip"),
    ("minimum 1st investment", r"(?i)min\. for 1st investment"),
    ("minimum 2nd investment", r"(?i)min\. for 2nd investment"),
    ("exit load", r"(?i)exit load"),
    ("risk rating", r"(?i)very high risk"),
    ("benchmark", r"(?i)fund benchmark"),
    ("investment objective", r"(?i)investment objective"),
    ("fund house", r"(?i)fund house"),
    ("custodian", r"(?i)custodian"),
    ("registrar", r"(?i)registrar & transfer agent"),
    ("stamp duty", r"(?i)stamp duty on investment"),
)


class _Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  PASS  {name}" + (f" — {detail}" if detail else ""))
        else:
            self.failed += 1
            print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
        return ok

    def section(self, title: str) -> None:
        print()
        print(title)
        print("-" * len(title))


def run_phase2() -> int:
    report = _Report()

    report.section("Phase 2 verification")
    print(f"Registry size: {len(SOURCES)}")

    documents, failures = loader.load_all()

    report.section("1. All five sources reachable")
    report.check(
        "no fetch failures",
        not failures,
        "; ".join(f"{u}: {e}" for u, e in failures) or "5/5 fetched",
    )
    report.check(
        "one document per registry source",
        len(documents) == len(SOURCES),
        f"{len(documents)}/{len(SOURCES)}",
    )
    for document in documents:
        report.check(
            f"HTTP 200 {document.source.slug}",
            document.status == 200,
            f"{document.status}, {document.html_bytes:,} bytes, "
            f"{len(document.units)} units",
        )

    if not documents:
        print()
        print("No documents fetched; cannot continue.")
        return 1

    chunks = chunker.build_corpus_chunks(documents)

    report.section("2. Registry containment")
    urls = {d.source.source_url for d in documents}
    report.check(
        "fetched URLs are exactly the registry",
        urls == {s.source_url for s in SOURCES},
        f"{len(urls)} URLs",
    )
    report.check(
        "every source URL is in the registry",
        all(is_allowed_url(d.source.source_url) for d in documents),
    )

    report.section("3. Chunk provenance")
    report.check("corpus is not empty", bool(chunks), f"{len(chunks)} chunks")
    report.check(
        "every chunk cites a registry URL",
        all(is_allowed_url(c.source_url) for c in chunks),
    )
    report.check(
        "chunk ids are unique",
        len({c.chunk_id for c in chunks}) == len(chunks),
        f"{len({c.chunk_id for c in chunks})} unique",
    )
    report.check(
        "every chunk carries all 9 metadata fields",
        all(
            all(
                getattr(c, field) not in (None, "")
                for field in (
                    "chunk_id", "source_url", "source_title", "scheme_name",
                    "doc_type", "section", "fetched_at", "content_hash",
                )
            )
            for c in chunks
        ),
    )

    report.section("4. Required facts present per document")
    for document in documents:
        text = "\n".join(u.text for u in document.units)
        missing = [
            name for name, pattern in REQUIRED_FACTS
            if not re.search(pattern, text)
        ]
        report.check(
            f"facts present: {document.source.slug}",
            not missing,
            f"missing {missing}" if missing else "all present",
        )

    report.section("5. ELSS lock-in captured")
    elss = next(
        (d for d in documents if "elss" in d.source.slug), None
    )
    if elss is None:
        report.check("ELSS document present", False)
    else:
        report.check(
            "ELSS lock-in unit present",
            any("lock-in" in u.text.lower() for u in elss.units),
            next(
                (u.text for u in elss.units if "lock-in" in u.text.lower()),
                "none found",
            ),
        )

    report.section("6. No empty or degenerate chunks")
    empty = [c.chunk_id for c in chunks if not c.body.strip()]
    report.check("no empty chunk", not empty, f"{len(empty)} empty")
    too_short = [
        c.chunk_id for c in chunks if len(c.body) < config.MIN_UNIT_CHARS
    ]
    report.check(
        "no chunk below the minimum length",
        not too_short,
        f"{len(too_short)} too short (min {config.MIN_UNIT_CHARS})",
    )
    longest = max(len(c.body) for c in chunks)
    report.check(
        "no chunk exceeds the hard cap",
        longest <= config.CHUNK_MAX_CHARS,
        f"longest {longest} chars (cap {config.CHUNK_MAX_CHARS})",
    )

    report.section("7. No nav, footer, or boilerplate")
    offenders = [
        c.chunk_id for c in chunks
        if any(term in c.body.lower() for term in BOILERPLATE)
    ]
    report.check("no boilerplate chunk", not offenders, f"{len(offenders)} found")

    report.section("8. Out-of-scope fields excluded")
    for pattern, name in FORBIDDEN_FIELDS:
        hits = [c.chunk_id for c in chunks if re.search(pattern, c.text, re.M)]
        report.check(f"no {name}", not hits, f"{len(hits)} hits")

    report.section("9. One fact or one labelled section per chunk")
    # A chunk qualifies if it is an inline `label: value` fact, a prose
    # paragraph belonging to one section, or a short badge/label whose meaning is
    # carried by the section field ("Very High Risk" under "Key facts"). What it
    # must never be is an unlabelled mixture of unrelated facts.
    inline_or_paragraph = [
        c.chunk_id for c in chunks
        if re.match(r"^[^:]{1,80}:\s*\S", c.body) or len(c.body) > 120
    ]
    unlabelled = [
        c.chunk_id for c in chunks
        if c.chunk_id not in set(inline_or_paragraph) and not c.section
    ]
    report.check(
        "every chunk is a labelled fact or a single section block",
        not unlabelled,
        f"{len(unlabelled)} unlabelled",
    )
    report.check(
        "every chunk has a section",
        all(c.section for c in chunks),
        f"{len({c.section for c in chunks})} distinct sections",
    )
    report.check(
        "every chunk names its own scheme",
        all(c.text.startswith(c.scheme_name) for c in chunks),
    )

    report.section("10. No out-of-scope scheme mentioned")
    for name in OUT_OF_SCOPE_SCHEMES:
        hits = [c.chunk_id for c in chunks if name.lower() in c.text.lower()]
        report.check(f"no {name}", not hits, f"{len(hits)} hits")

    report.section("11. No out-of-scope phase work was done")
    # Phase 4+ modules must not exist yet. Ingestion is not allowed to reach
    # ahead, and this stays a meaningful check once Phase 3 creates the store.
    #
    # This guard is only meaningful *during* the ordered build. Once Phases 4-6
    # have run, those modules exist precisely because they were supposed to, so
    # re-running Phase 2 standalone reports a failure for correct code. Rather
    # than delete the check or let it fail forever, it is skipped with the
    # reason stated, so a later re-run neither cries wolf nor hides the check.
    future = [
        name for name in
        ("guardrails.py", "retrieval.py", "prompt.py", "llm.py", "render.py")
        if (PROJECT_ROOT / "src" / name).exists()
    ]
    if future and (PROJECT_ROOT / "app.py").exists():
        print(
            "  SKIP  no Phase 4+ modules exist — later phases have run, so their "
            f"modules are expected: {', '.join(future)}"
        )
    else:
        report.check(
            "no Phase 4+ modules exist",
            not future,
            f"present: {future}" if future else "none",
        )

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


def run_phase3() -> int:
    """Delegate to the Phase 3 suite, kept in its own module.

    Imported lazily because the Phase 3 checks import `_Report` from here.
    """
    from src import verify_phase3

    return verify_phase3.run_phase3()


def run_phase4() -> int:
    """Delegate to the Phase 4 guardrail suite.

    Imported lazily to keep the Phase 2/3 suites independent of it.
    """
    from src import verify_phase4

    return verify_phase4.run_phase4()


def run_phase5() -> int:
    """Delegate to the Phase 5 retrieval + LLM suite.

    Imported lazily so the Phase 2/3/4 suites stay independent of the query
    path, the embedding model, and the store.
    """
    from src import verify_phase5

    return verify_phase5.run_phase5()


def run_phase6() -> int:
    """Delegate to the Phase 6 UI + deliverables suite.

    Imported lazily because it is the only suite that imports Streamlit and
    launches a server, and the earlier phases must stay runnable without either.
    """
    from src import verify_phase6

    return verify_phase6.run_phase6()


__all__ = ["run_phase2", "run_phase3", "run_phase4", "run_phase5", "run_phase6"]
