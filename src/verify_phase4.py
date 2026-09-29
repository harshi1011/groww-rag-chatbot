"""Phase 4 verification — the 10 checks from doc/implementation.md.

Run with:

    .venv\\Scripts\\python.exe ingest.py --verify-only --phase 4

Every function under test here is pure: no store, no model, no network. The
suite deliberately imports only `src.guardrails` and `src.config`, which is
itself part of the separation being verified.
"""

from __future__ import annotations

import io
import logging
import re
import sys
from contextlib import redirect_stdout

from src import config, guardrails
from src.config import PROJECT_ROOT

# --------------------------------------------------------------------------
# Fixtures — PRD §6 verbatim, plus the shapes implementation.md names.
# --------------------------------------------------------------------------
PII_CASES: tuple[tuple[str, str], ...] = (
    ("pan", "My PAN is ABCDE1234F, what is the exit load?"),
    ("aadhaar", "My Aadhaar number is 1234 5678 9012, please check the exit load"),
    ("long_digit_run", "Update my account number 1234567890 for the same fund"),
    ("keyword", "My OTP is 482913, can you check the expense ratio?"),
    ("email", "Send the statement to rakesh.sharma@example.com please"),
    ("phone", "Call me on +91 9876543210 about the minimum SIP"),
)

#: Digit-bearing but entirely harmless. A PII screen that blocks these is a
#: defect, not caution.
PII_NEGATIVE_CASES: tuple[tuple[str, str], ...] = (
    ("lock-in in years", "What is the lock-in period in years for the ELSS fund?"),
    ("percentage", "What percentage is the expense ratio of the HDFC Large Cap Fund?"),
    ("small amount", "Is the minimum SIP 500 rupees or less for the Small Cap Fund?"),
    ("year count", "Is the fund benchmarked over 3 years against Nifty 50?"),
    ("date", "What is the date of incorporation of the Balanced Advantage Fund?"),
)

REFUSAL_CASES: tuple[tuple[str, str], ...] = (
    ("should I buy", "Should I buy the HDFC Small Cap Fund?", "advice"),
    ("good time to sell", "Is now a good time to sell my ELSS?", "advice"),
    ("portfolio choice", "Which of these five funds should be in my portfolio?",
     "advice"),
    ("best returns", "Which fund has the best returns?", "performance"),
    ("safety for a goal", "Is HDFC Large Cap safe for a 3-year goal?", "advice"),
    ("returns comparison", "Compare the returns of HDFC Large Cap and HDFC Small Cap",
     "performance"),
    ("other AMC", "What is the expense ratio of the Parag Parikh Flexi Cap Fund?",
     "out-of-scheme"),
    ("other asset class", "How do I buy cryptocurrency with my mutual fund money?",
     "off-topic"),
)

#: The 10 answerable questions from PRD §6. Every one must classify as
#: `factual`. A single refusal here means the intent guard is too aggressive.
FACTUAL_CASES: tuple[str, ...] = (
    "What is the expense ratio of the HDFC Large Cap Fund Direct Growth?",
    "What is the exit load on the HDFC Equity Fund Direct Growth?",
    "What is the minimum SIP amount for the HDFC Small Cap Fund Direct Growth?",
    "What is the lock-in period for the HDFC ELSS Tax Saver Fund Direct Plan Growth?",
    "What is the riskometer level and benchmark of the HDFC Balanced Advantage Fund "
    "Direct Growth?",
    "How do I download my capital-gains statement?",
    "What are the management fees / other charges listed for the HDFC Large Cap Fund?",
    "What is the minimum lump-sum investment amount for the HDFC ELSS Tax Saver Fund?",
    "Where can I find the SID / KIM for the HDFC Equity Fund?",
    "What documents are needed for tax reporting on these equity-oriented funds?",
)

#: The exact substrings that must never appear anywhere after a PII case runs.
SECRET_FRAGMENTS: tuple[str, ...] = (
    "ABCDE1234F", "1234 5678 9012", "1234567890", "482913",
    "rakesh.sharma@example.com", "9876543210",
)

GOOD_ANSWER = (
    "The expense ratio of the HDFC Large Cap Fund Direct Growth is 1.03%. "
    "The minimum for the first investment is Rs 100."
)
GOOD_CHUNK_ID = "hdfc-large-cap-fund-direct-growth-000"

BAD_ANSWERS: tuple[tuple[str, str, str], ...] = (
    ("five sentences", GOOD_CHUNK_ID,
     "The expense ratio is 1.03%. The minimum SIP is Rs 500. The exit load is nil. "
     "The fund was launched in 1996. The benchmark is the Nifty 50 NTR index."),
    ("bare URL", GOOD_CHUNK_ID,
     "The expense ratio is 1.03%. See https://example.com/fund for details."),
    ("unknown citation", "not-a-real-chunk-id-999",
     "The expense ratio is 1.03%. The minimum SIP is Rs 500."),
    ("recommendation phrasing", GOOD_CHUNK_ID,
     "You should invest in this fund. The expense ratio is 1.03%."),
    ("return figure", GOOD_CHUNK_ID,
     "The fund has returned 18.4% per year over 3 years."),
    ("ranking two schemes", GOOD_CHUNK_ID,
     "HDFC Large Cap is the better fund compared with HDFC Small Cap on performance."),
)


def run_phase4() -> int:
    from src.verify import _Report

    report = _Report()
    report.section("Phase 4 verification")
    print(f"disclaimer: {config.DISCLAIMER!r}")

    # -- 1. PII positives --------------------------------------------------
    report.section("1. PII positives (all blocked)")
    for name, text in PII_CASES:
        result = guardrails.screen_pii(text)
        decision = guardrails.screen_input(text)
        report.check(
            f"PII blocked: {name}",
            result.blocked and not decision.ok,
            f"categories={result.categories}",
        )

    # -- 2. PII negatives (over-blocking) ----------------------------------
    report.section("2. PII negatives (harmless digit questions not blocked)")
    for name, text in PII_NEGATIVE_CASES:
        result = guardrails.screen_pii(text)
        report.check(
            f"not blocked: {name}",
            not result.blocked,
            f"categories={result.categories}" if result.blocked else "clean",
        )
    for name, text in PII_NEGATIVE_CASES:
        decision = guardrails.screen_input(text)
        report.check(
            f"passes the full screen: {name}",
            decision.ok,
            f"intent={decision.intent.category}",
        )

    # -- 3. no echo, no log ------------------------------------------------
    report.section("3. No echo, no log (SC-8)")
    logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger("src").setLevel(logging.DEBUG)
    captured = io.StringIO()
    with redirect_stdout(captured):
        decisions = [guardrails.screen_input(text) for _, text in PII_CASES]
        pii_results = [guardrails.screen_pii(text) for _, text in PII_CASES]
    logged = captured.getvalue()
    report.check(
        "no secret reaches stdout",
        not any(secret in logged for secret in SECRET_FRAGMENTS),
        f"{len(logged)} chars captured",
    )
    rendered = logged + " ".join(
        [repr(d) for d in decisions] + [repr(r) for r in pii_results]
    )
    leaked = [s for s in SECRET_FRAGMENTS if s in rendered]
    report.check(
        "no secret in any return object",
        not leaked,
        f"leaked {leaked}" if leaked else "none",
    )
    report.check(
        "PII refusal message is the fixed constant",
        all(d.message == config.PII_MESSAGE for d in decisions),
    )
    report.check(
        "PII refusal carries no link and no reason",
        all(not d.link_url and d.intent.category == "pii" for d in decisions),
    )
    report.check(
        "the raw question is not retained on the result",
        not any(
            text in repr(d) for d in decisions for _, text in PII_CASES
        ),
    )

    # -- 4. intent refusals ------------------------------------------------
    report.section("4. Intent — refusals return fixed strings, no LLM call")
    for name, text, expected in REFUSAL_CASES:
        decision = guardrails.screen_input(text)
        report.check(
            f"refused as {expected}: {name}",
            not decision.ok and decision.intent.category == expected,
            f"got {decision.intent.category}",
        )
        report.check(
            f"fixed string + educational link: {name}",
            bool(decision.message) and bool(decision.link_url),
            f"{decision.link_label} -> {decision.link_url}",
        )
    report.check(
        "refusals use the configured constants verbatim",
        all(
            d.message in {
                config.REFUSAL_ADVICE, config.REFUSAL_PERFORMANCE,
                config.OUT_OF_SCOPE,
            }
            for _, text, _ in REFUSAL_CASES
            for d in [guardrails.screen_input(text)]
        ),
    )
    report.check(
        "no refusal contains recommendation phrasing",
        not any(
            guardrails._matches_any(
                d.message, config.OUTPUT_ADVICE_PATTERNS
            )
            for _, text, _ in REFUSAL_CASES
            for d in [guardrails.screen_input(text)]
        ),
    )
    report.check(
        "no LLM client is imported by the refusal path",
        "groq" not in sys.modules and "src.llm" not in sys.modules,
        "groq not loaded",
    )
    # Phase 4 asserted these modules did not exist, which was true then and is
    # false after Phase 5 built them. The property that actually matters is not
    # file existence but that the guardrails stay independent of them: a refusal
    # must be able to be produced without an LLM or a store in the process.
    guardrail_source = (PROJECT_ROOT / "src" / "guardrails.py").read_text(
        encoding="utf-8"
    )
    report.check(
        "the guardrails module imports no LLM, retrieval, store, or model",
        not re.search(
            r"^\s*(?:from\s+src(?:\.\w+)*\s+)?import\s+"
            r"[^\n]*\b(?:llm|retrieval|prompt|render|answer|vectorstore|embedder)\b",
            guardrail_source,
            re.M,
        ),
    )

    # -- 5. intent factual (false positives) -------------------------------
    report.section("5. Intent — all 10 PRD factual questions pass")
    false_positives = [
        text for text in FACTUAL_CASES
        if not guardrails.screen_input(text).ok
    ]
    report.check(
        "zero false positives across the 10 factual questions",
        not false_positives,
        f"{len(false_positives)} refused"
        if false_positives
        else "10/10 factual",
    )
    for text in false_positives:
        decision = guardrails.screen_input(text)
        print(f"      refused: {text!r} as {decision.intent.category}")
    report.check(
        "every factual question classifies as 'factual'",
        all(
            guardrails.classify_intent(t).category == "factual"
            for t in FACTUAL_CASES
        ),
    )

    # -- 6. output accept --------------------------------------------------
    report.section("6. Output checks — accept the good fixture")
    valid_ids = _valid_chunk_ids()
    accepted = guardrails.check_output(GOOD_ANSWER, GOOD_CHUNK_ID, valid_ids)
    report.check(
        "well-formed 2-sentence answer with a valid citation passes",
        accepted.ok,
        f"problems={accepted.problems}",
    )
    report.check(
        "sentence count is read as 2",
        accepted.sentence_count == 2,
        str(accepted.sentence_count),
    )
    report.check(
        "a 1.03% expense ratio is not mistaken for a return",
        accepted.ok,
    )

    # -- 7. output rejects -------------------------------------------------
    report.section("7. Output checks — reject the bad fixtures")
    for name, chunk_id, answer in BAD_ANSWERS:
        result = guardrails.check_output(answer, chunk_id, valid_ids)
        report.check(
            f"rejected: {name}",
            not result.ok,
            f"problems={list(result.problems)}",
        )
    report.check(
        "a missing citation is rejected",
        not guardrails.check_output(GOOD_ANSWER, None, valid_ids).ok,
    )
    report.check(
        "an empty answer is rejected",
        not guardrails.check_output("   ", GOOD_CHUNK_ID, valid_ids).ok,
    )
    report.check(
        "the 3-sentence limit is inclusive",
        guardrails.check_output(
            "One fact. Two facts. Three facts.", GOOD_CHUNK_ID, valid_ids
        ).ok,
    )

    # -- 8. fallback -------------------------------------------------------
    report.section("8. Fallback path (extractive, no generated text)")
    chunk_text = (
        "HDFC Large Cap Fund Direct Growth — Key facts: Expense ratio: 1.03%. "
        "Min. for SIP: Rs 500. Exit load: Nil. Fund size (AUM): Rs 1,23,456 crore."
    )
    fallback = guardrails.build_fallback_answer(chunk_text)
    report.check(
        "fallback is extractive from the top chunk",
        fallback and all(s in chunk_text for s in
                         guardrails.split_sentences(fallback)),
        f"{fallback!r}",
    )
    report.check(
        "fallback is within the 3-sentence limit",
        guardrails.count_sentences(fallback) <= config.MAX_ANSWER_SENTENCES,
        f"{guardrails.count_sentences(fallback)} sentences",
    )
    report.check(
        "fallback passes its own output guard",
        guardrails.check_output(fallback, GOOD_CHUNK_ID, valid_ids).ok,
    )
    report.check(
        "fallback is deterministic (same input, same output)",
        guardrails.build_fallback_answer(chunk_text) == fallback,
    )
    report.check(
        "fallback of an empty chunk is empty, not invented",
        guardrails.build_fallback_answer("") == "",
    )

    # -- 9. fixed-string integrity ----------------------------------------
    report.section("9. Fixed-string integrity")
    report.check(
        "disclaimer is exactly 'Facts-only. No investment advice.'",
        config.DISCLAIMER == "Facts-only. No investment advice.",
        repr(config.DISCLAIMER),
    )
    report.check(
        "educational links are configured constants, not empty",
        bool(config.EDUCATIONAL_LINKS) and all(
            u.startswith("https://") for u in config.EDUCATIONAL_LINKS.values()
        ),
        f"{len(config.EDUCATIONAL_LINKS)} links",
    )
    off_allowlist = [
        url for url in config.EDUCATIONAL_LINKS.values()
        if re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        not in config.EDUCATIONAL_LINK_DOMAINS
    ]
    report.check(
        "every link points at a known public domain",
        not off_allowlist,
        f"off-allowlist: {off_allowlist}" if off_allowlist else "all allowlisted",
    )
    report.check(
        "no educational link is empty or whitespace",
        all(v.strip() for v in config.EDUCATIONAL_LINKS.values()),
    )

    # -- 10. separation ----------------------------------------------------
    report.section("10. Separation check")
    app_text = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+(\S+)", app_text, re.M)
    leaked = [
        name for name in imports
        if any(p in name for p in (
            "loader", "chunker", "vectorstore", "embedder", "guardrails",
            "retrieval", "llm", "urllib",
        ))
    ]
    report.check(
        "app.py imports no ingestion or query-path module",
        not leaked,
        f"imports {sorted(set(imports))}",
    )
    guard_text = (PROJECT_ROOT / "src" / "guardrails.py").read_text(
        encoding="utf-8"
    )
    report.check(
        "guardrails imports no store, model, or network module",
        not re.search(
            r"^\s*(?:from|import)\s+(?:src\.(?:vectorstore|embedder|loader|"
            r"chunker|retrieval|llm)|chromadb|sentence_transformers|urllib|groq)",
            guard_text,
            re.M,
        ),
    )
    report.check(
        "guardrails defines the grounding rule for the Phase 5 prompt",
        "only from the supplied context" in guardrails.GROUNDING_RULES,
    )
    report.check(
        "grounding rejects a number absent from the context",
        guardrails.check_grounding(
            "The expense ratio is 2.50%.", "Expense ratio: 1.03%."
        ),
    )
    report.check(
        "grounding accepts a supported answer",
        not guardrails.check_grounding(
            "The expense ratio is 1.03%.", "Expense ratio: 1.03%."
        ),
    )
    report.check(
        "grounding rejects an answer sharing nothing with the context",
        guardrails.check_grounding("Bitcoin is decentralised.", "Expense ratio: 1.03%."),
    )

    report.section("Relevance gate (threshold calibrated in Phase 5)")
    # Phase 4 shipped with the threshold unset so the gate could not silently
    # decide what the bot claims not to know. Phase 5 calibrated it from the
    # observed score distribution, so the assertion is inverted: the gate must
    # now use the frozen value, and the no-explicit-argument path must work.
    try:
        default_gate = guardrails.check_relevance([0.1, 0.4])
        detail = f"threshold={default_gate.threshold}"
        calibrated = default_gate.threshold == config.RELEVANCE_THRESHOLD
    except Exception as exc:
        calibrated = False
        detail = f"{type(exc).__name__}: {str(exc)[:60]}"
    report.check(
        "the gate now uses the Phase 5 calibrated threshold",
        calibrated,
        detail,
    )
    report.check(
        "a missing config threshold still fails loudly instead of guessing",
        _unset_threshold_fails_loudly(),
    )
    report.check(
        "empty result set is not relevant",
        not guardrails.check_relevance([], threshold=0.5).relevant,
    )
    report.check(
        "top-1 under the threshold is relevant",
        guardrails.check_relevance([0.2, 0.9], threshold=0.5).relevant,
    )
    report.check(
        "every distance over the threshold is not relevant",
        not guardrails.check_relevance([0.8, 0.9], threshold=0.5).relevant,
    )
    report.check(
        "top-1 is the smallest distance, not the first result",
        guardrails.check_relevance(
            [0.8, 0.2], threshold=0.5
        ).best_score == 0.2,
    )
    report.check(
        "a single result exactly at the threshold is relevant",
        guardrails.check_relevance([0.5], threshold=0.5).relevant,
    )
    report.check(
        "not-relevant response is the fixed NO_ANSWER string",
        config.NO_ANSWER.startswith("I do not have that in my sources."),
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


def _unset_threshold_fails_loudly() -> bool:
    """The guard's fail-loudly behaviour, proven with the value temporarily unset.

    Asserted directly instead of by unsetting the real config, so this check
    cannot be satisfied or broken by the calibration value itself.
    """
    original = config.RELEVANCE_THRESHOLD
    try:
        config.RELEVANCE_THRESHOLD = None
        guardrails.check_relevance([0.1, 0.4])
    except config.ConfigError:
        return True
    except Exception:
        return False
    finally:
        config.RELEVANCE_THRESHOLD = original
    return False


def _valid_chunk_ids() -> frozenset[str]:
    """Real chunk ids from the Phase 2 artefact, so an unknown id is genuinely
    unknown. Read from the text file rather than the store, which keeps this
    suite runnable with no store and no model."""
    if not config.CHUNKS_TXT.exists():
        return frozenset({GOOD_CHUNK_ID})
    ids = re.findall(
        r"^\[([a-z0-9-]+-\d{3})\]",
        config.CHUNKS_TXT.read_text(encoding="utf-8"),
        re.M,
    )
    return frozenset(ids) or frozenset({GOOD_CHUNK_ID})


__all__ = ["run_phase4"]
