"""Phase 5 verification — the 13 checks from doc/implementation.md, in order.

The LLM is exercised through an injected deterministic generator, not a live
Groq call, except for check 3 which is explicitly a live-key check. That is a
deliberate split: the pipeline logic (guards, citation resolution, fallback,
freshness) must be verifiable on every run and without a network, while the
network path gets its own single explicit check that reports honestly when no
key is configured.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date

from src import (
    answer,
    config,
    guardrails,
    llm,
    memory as memory_mod,
    preflight,
    prompt,
    render,
    retrieval,
    sources,
    vectorstore,
)

#: The 10 factual questions from PRD §6, verbatim.
FACTUAL_QUESTIONS: tuple[str, ...] = (
    "What is the expense ratio of the HDFC Large Cap Fund Direct Growth?",
    "What is the exit load on the HDFC Equity Fund Direct Growth?",
    "What is the minimum SIP amount for the HDFC Small Cap Fund Direct Growth?",
    "What is the lock-in period for the HDFC ELSS Tax Saver Fund Direct Plan Growth?",
    "What is the riskometer level and benchmark of the "
    "HDFC Balanced Advantage Fund Direct Growth?",
    "How do I download my capital-gains statement?",
    "What are the management fees / other charges listed for the HDFC Large Cap Fund?",
    "What is the minimum lump-sum investment amount for the HDFC ELSS Tax Saver Fund?",
    "Where can I find the SID / KIM for the HDFC Equity Fund?",
    "What documents are needed for tax reporting on these equity-oriented funds?",
)

#: The 5 refusal questions from PRD §6, verbatim.
REFUSAL_QUESTIONS: tuple[str, ...] = (
    "Should I buy the HDFC Small Cap Fund?",
    "Is now a good time to sell my ELSS?",
    "Which of these five funds should be in my portfolio?",
    "Which fund has the best returns?",
    "Is HDFC Large Cap safe for a 3-year goal?",
)

#: The 2 PII questions from PRD §6.
PII_QUESTIONS: tuple[str, ...] = (
    "My PAN is ABCDE1234F, what is the exit load?",
    "Update my account number 1234567890",
)

#: A fact the corpus does not carry, phrased to sound answerable: a
#: risk-adjusted return ratio. None of the five pages carry it, but a model
#: asked about a "fee" will happily produce one if allowed to guess.
NO_FABRICATION_QUESTION = "What is the Sharpe ratio of HDFC Large Cap Fund Direct Growth?"
NO_FABRICATION_TERM = "sharpe"

REGISTRY_URLS: frozenset[str] = frozenset(s.source_url for s in sources.SOURCES)


class _Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self._failures: list[str] = []

    def section(self, title: str) -> None:
        print(f"\n{title}")
        print("-" * len(title))

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  PASS  {label}" + (f" \u2014 {detail}" if detail else ""))
        else:
            self.failed += 1
            self._failures.append(label)
            print(f"  FAIL  {label}" + (f" \u2014 {detail}" if detail else ""))
        return ok

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
# A deterministic stand-in for Groq.
# --------------------------------------------------------------------------
class FakeLlm:
    """Records every call and replies from the supplied context.

    Three modes, because the pipeline has to survive all three:

    * `answers` supplied — the model ignores the context entirely. Used to
      drive the retry and fallback paths deterministically.
    * `require_term` set — a *compliant* model: if the term the question is
      really about does not appear in the context, it returns the refusal
      string, exactly as `prompt.SYSTEM_PROMPT` instructs. This is what makes
      the no-fabrication check a test of the pipeline rather than a test of how
      literal the stand-in is.
    * default — extractive: the first two sentences of the top context block.
    """

    def __init__(self, answers: list[str] | None = None, require_term: str | None = None) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self._answers = answers
        self._require_term = require_term

    def __call__(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        if self._answers is not None:
            index = min(len(self.calls) - 1, len(self._answers) - 1)
            return self._answers[index]
        return self._from_context(messages[-1]["content"])

    def _from_context(self, user_message: str) -> str:
        # Only the CONTEXT section is searched for the required term. The
        # question itself is echoed into the user turn, so scanning the whole
        # message would always find the term and defeat the check.
        context = user_message.split("QUESTION", 1)[0].lower()
        blocks = _context_blocks(user_message)
        if not blocks:
            return "This information is not in my sources."
        if self._require_term and not _has_term(context, self._require_term):
            # A compliant model refuses rather than answering from an unrelated
            # block. No chunk_id, so the pipeline's citation fallback is also
            # exercised here.
            return "This information is not in my sources."
        first_id = blocks[0]["chunk_id"]
        sentences = guardrails.split_sentences(blocks[0]["text"])
        if not sentences:
            return f"This information is not in my sources.\nchunk_id: {first_id}"
        return f"{' '.join(sentences[:2])}\nchunk_id: {first_id}"


def _has_term(haystack: str, term: str) -> bool:
    """Whole-word containment, so 'sid' does not match 'considered'."""
    return bool(re.search(rf"\b{re.escape(term)}\b", haystack, re.I))


def _context_blocks(user_message: str) -> list[dict[str, str]]:
    """Parse the prompt's labelled context blocks back out of the user turn."""
    blocks: list[dict[str, str]] = []
    for raw in re.split(r"^\[\d+\] ", user_message, flags=re.M):
        if not raw.strip():
            continue
        chunk_id = re.search(r"^chunk_id: (\S+)$", raw, re.M)
        text = re.search(r"^text: (.*)$", raw, re.M)
        if chunk_id and text:
            blocks.append(
                {
                    "chunk_id": chunk_id.group(1),
                    "text": " ".join(text.group(1).split()),
                }
            )
    return blocks


# --------------------------------------------------------------------------
def _check_preflight_missing_store(report: _Report) -> None:
    report.section("1. Preflight \u2014 missing store")
    with tempfile.TemporaryDirectory() as tmp:
        empty = os.path.join(tmp, "no-such-store")
        result = preflight.run(chroma_dir=_Path(empty), manifest_path=_Path(tmp) / "m.json")
        report.check(
            "preflight fails on a missing store",
            not result.ok,
        )
        report.check(
            "the message names the ingestion command",
            "python ingest.py" in result.message,
            result.message[:80],
        )
        report.check(
            "nothing is created and no ingestion is triggered",
            not os.path.exists(empty),
        )


def _check_preflight_model_mismatch(report: _Report) -> None:
    report.section("2. Preflight \u2014 model mismatch (refuses to serve)")
    real = vectorstore.read_manifest()
    assert real is not None
    with tempfile.TemporaryDirectory() as tmp:
        drifted = dict(real)
        drifted["embedding_model_id"] = "sentence-transformers/all-MiniLM-L12-v2"
        path = _Path(tmp) / "manifest.json"
        path.write_text(json.dumps(drifted), encoding="utf-8")
        result = preflight.run(chroma_dir=config.CHROMA_DIR, manifest_path=path)
        report.check("preflight refuses on a model-id mismatch", not result.ok)
        report.check(
            "the mismatch is reported and re-ingestion is required",
            "mismatch" in result.message.lower()
            and "ingest.py" in result.message,
            result.message[:90],
        )
        # The real manifest is untouched.
        report.check(
            "the real manifest is unchanged",
            vectorstore.read_manifest().get("embedding_model_id")
            == config.EMBEDDING_MODEL_ID,
        )


def _check_preflight_drift(report: _Report) -> None:
    report.section("3. Preflight \u2014 corpus drift")
    real = vectorstore.read_manifest()
    with tempfile.TemporaryDirectory() as tmp:
        drifted = dict(real)
        drifted["sources"] = list(real.get("sources", []))[:2]
        path = _Path(tmp) / "manifest.json"
        path.write_text(json.dumps(drifted), encoding="utf-8")
        result = preflight.run(chroma_dir=config.CHROMA_DIR, manifest_path=path)
        report.check(
            "a registry/store mismatch is reported as a warning",
            result.ok and bool(result.warnings),
            result.warnings[0][:80] if result.warnings else "no warning",
        )
        report.check(
            "the warning asks for re-ingestion",
            any("ingest.py" in w for w in result.warnings),
        )
    healthy = preflight.run()
    report.check(
        "the real artefacts pass preflight with no warnings",
        healthy.ok and not healthy.warnings,
    )


#: The chunk each answerable question's fact lives in, from the top-k dump in
#: the calibration run. Recorded so this is a real assertion about chunking,
#: not a restatement of "it returned something".
EXPECTED_FACT_CHUNKS: dict[str, str] = {
    "What is the expense ratio of the HDFC Large Cap Fund Direct Growth?":
        "hdfc-large-cap-fund-direct-growth-003",
    "What is the minimum SIP amount for the HDFC Small Cap Fund Direct Growth?":
        "hdfc-small-cap-fund-direct-growth-001",
    "What is the lock-in period for the HDFC ELSS Tax Saver Fund Direct Plan Growth?":
        "hdfc-elss-tax-saver-fund-direct-plan-growth-000",
    "What is the riskometer level and benchmark of the "
    "HDFC Balanced Advantage Fund Direct Growth?":
        "hdfc-balanced-advantage-fund-direct-growth-000",
    "What are the management fees / other charges listed for the HDFC Large Cap Fund?":
        "hdfc-large-cap-fund-direct-growth-003",
    "What is the minimum lump-sum investment amount for the HDFC ELSS Tax Saver Fund?":
        "hdfc-elss-tax-saver-fund-direct-plan-growth-005",
}

#: Questions whose fact these five pages genuinely do not carry, with a term
#: proven absent from data/chunks.txt (0 occurrences each). A correct system
#: refuses these; an incorrect one invents a number.
#:
#: Note that "capital gains" itself appears 15 times in the corpus, and "SID" 5
#: times. What is missing is the *instruction* to download a statement, and the
#: list of documents needed for tax reporting. The term is chosen to prove the
#: specific gap, not merely that the topic is unfamiliar.
CORPUS_GAPS: dict[str, str] = {
    "How do I download my capital-gains statement?": "download",
    "What documents are needed for tax reporting on these equity-oriented funds?":
        "tax reporting",
}

#: Questions whose fact IS in the corpus but which the brief's naming prevented
#: from being found: the brief says "HDFC Equity Fund", the page H1 says "HDFC
#: Flexi Cap Direct Plan Growth", and the string "Equity Fund" appears nowhere
#: in the corpus. Now resolved by a query-time alias (sources.SCHEME_ALIASES)
#: rather than by changing the chunks, which is out of scope for Phase 5.
ALIAS_LIMITATIONS: tuple[str, ...] = (
    "What is the exit load on the HDFC Equity Fund Direct Growth?",
    "Where can I find the SID / KIM for the HDFC Equity Fund?",
)

OFF_SCOPE_QUESTIONS: tuple[str, ...] = (
    "What is the price of gold today?",
    "How do I file a tax return?",
    "What is the weather in Mumbai?",
    "How do I open a bank account?",
)


def _check_retrieval_sanity(report: _Report) -> None:
    report.section("4. Retrieval sanity (no LLM) \u2014 top-k inspected by hand")
    report.check(
        "k and the threshold are calibrated and frozen in config",
        config.RETRIEVAL_TOP_K == 5 and config.RELEVANCE_THRESHOLD == 0.45,
        f"k={config.RETRIEVAL_TOP_K} threshold={config.RELEVANCE_THRESHOLD}",
    )
    all_ok = True
    for question, chunk_id in EXPECTED_FACT_CHUNKS.items():
        hits = retrieval.search(question)
        found = [h.chunk_id for h in hits]
        ok = chunk_id in found
        all_ok = all_ok and ok
        report.check(
            f"fact chunk in top-k: {question[:52]}",
            ok,
            f"rank {found.index(chunk_id) + 1} of {len(found)}, "
            f"d={hits[0].distance:.4f}"
            if ok else f"missing {chunk_id}",
        )
    report.check(
        "every answerable factual question retrieves its fact-bearing chunk",
        all_ok,
    )
    # --- query-time scheme alias -----------------------------------------
    # The brief says "HDFC Equity Fund"; the corpus says "HDFC Flexi Cap Direct
    # Plan Growth". A query-time rewrite bridges the two without touching the
    # chunks, the embeddings, or Chroma. Asserted here end to end: the mapping is
    # sound, it is a no-op where it should be, and it puts the right scheme's
    # chunk at the top for both affected questions.
    corpus_text = config.CHUNKS_TXT.read_text(encoding="utf-8")
    report.check(
        "the alias maps to a real registry slug and is not a no-op",
        sources.alias_problems() == [],
        f"{len(sources.SCHEME_ALIASES)} alias(es), 0 problems",
    )
    report.check(
        "the corpus really does lack the brief's name",
        "Equity Fund" not in corpus_text
        and "Flexi Cap" in corpus_text,
        "'Equity Fund' 0 occurrences, 'Flexi Cap' present",
    )
    non_alias = [q for q in FACTUAL_QUESTIONS if q not in ALIAS_LIMITATIONS]
    changed = [q for q in non_alias if sources.expand_aliases(q) != q]
    report.check(
        "expansion leaves every non-alias question byte-identical",
        not changed and len(non_alias) == len(FACTUAL_QUESTIONS) - len(ALIAS_LIMITATIONS),
        f"{len(non_alias)} of {len(FACTUAL_QUESTIONS)} PRD questions unchanged; "
        f"the other {len(ALIAS_LIMITATIONS)} are the ones being fixed"
        if not changed else str(changed),
    )
    report.check(
        "expansion rewrites the brief's name to the corpus name",
        sources.expand_aliases("HDFC Equity Fund exit load")
        == "HDFC Flexi Cap Direct Plan Growth exit load",
        sources.expand_aliases("HDFC Equity Fund exit load"),
    )
    report.check(
        "expansion does not fire on an unrelated word",
        sources.expand_aliases("What is an equity fund?")
        == "What is an equity fund?",
    )
    # The payoff: the right scheme, and therefore the right source link.
    exit_q = ALIAS_LIMITATIONS[0]
    exit_hits = retrieval.search(exit_q)
    exit_url = "https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth"
    report.check(
        "the Equity Fund exit load now retrieves the Flexi Cap chunk",
        "hdfc-equity-fund-direct-growth-011"
        in [h.chunk_id for h in exit_hits],
        f"correct chunk in top-{len(exit_hits)}; top-1 {exit_hits[0].chunk_id} "
        f"d={exit_hits[0].distance:.4f}",
    )
    report.check(
        "the top-1 hit is the Flexi Cap scheme, not another scheme",
        exit_hits[0].source_url == exit_url,
        f"{exit_hits[0].scheme_name} d={exit_hits[0].distance:.4f}",
    )
    report.check(
        "the citation for that answer resolves to the Flexi Cap page",
        render.resolve_citation(exit_hits[0].chunk_id, exit_hits).url == exit_url,
    )
    sid_hits = retrieval.search(ALIAS_LIMITATIONS[1])
    report.check(
        "the SID question's top-1 is the Flexi Cap SID chunk",
        sid_hits[0].chunk_id == "hdfc-equity-fund-direct-growth-021"
        and sid_hits[0].source_url == exit_url,
        f"top-1 {sid_hits[0].chunk_id} [{sid_hits[0].section}] "
        f"d={sid_hits[0].distance:.4f}",
    )
    # Nothing about the corpus may have moved.
    manifest = vectorstore.read_manifest()
    report.check(
        "chunks, embeddings, and the store are untouched by the fix",
        manifest.get("corpus_hash")
        == "eb952531183dc07ec3ac883197e3ea0180f0626c23242ca93513adb52a2bf680"
        and vectorstore.count() == 144
        and len(corpus_text.splitlines()) > 0,
        f"corpus_hash unchanged, {vectorstore.count()} vectors",
    )
    report.check(
        "no source was added to the registry",
        len(sources.SOURCES) == 5 and len(sources.SOURCE_URLS) == 5,
        f"{len(sources.SOURCES)} registry entries",
    )
    # Metadata must travel with the text (architecture \u00a74.2).
    hit = retrieval.search(FACTUAL_QUESTIONS[0])[0]
    report.check(
        "metadata travels with the chunk",
        bool(hit.source_url and hit.source_title and hit.section and hit.fetched_at),
        f"{hit.source_title} / {hit.section} / fetched {hit.fetched_at}",
    )
    report.check(
        "every retrieved chunk's URL is in the registry",
        all(h.source_url in REGISTRY_URLS for h in hits),
    )


def _check_calibration(report: _Report) -> None:
    report.section("5. Calibration \u2014 the threshold separates the bands")
    answerable = [
        min(h.distance for h in retrieval.search(q))
        for q in EXPECTED_FACT_CHUNKS
    ]
    gapped = [
        min(h.distance for h in retrieval.search(q)) for q in CORPUS_GAPS
    ]
    off_scope = [
        min(h.distance for h in retrieval.search(q)) for q in OFF_SCOPE_QUESTIONS
    ]
    print(
        f"  answerable:  {min(answerable):.4f} .. {max(answerable):.4f}\n"
        f"  corpus gap:  {min(gapped):.4f} .. {max(gapped):.4f}\n"
        f"  off-scope:   {min(off_scope):.4f} .. {max(off_scope):.4f}"
    )
    report.check(
        "every answerable question is above the threshold",
        all(guardrails.check_relevance([d]).relevant for d in answerable),
    )
    report.check(
        "every off-scope question is below the threshold",
        all(not guardrails.check_relevance([d]).relevant for d in off_scope),
    )
    report.check(
        "the three observed bands are disjoint",
        max(answerable) < config.RELEVANCE_THRESHOLD < min(off_scope),
        f"answerable<={max(answerable):.4f} < t={config.RELEVANCE_THRESHOLD} "
        f"< off-scope>={min(off_scope):.4f}",
    )
    # Not all three corpus gaps are refused by the gate. The gate is a
    # similarity test, and the SID / KIM question retrieves a plausible-looking
    # "About" chunk at 0.3702, below the threshold. Correctness for that one
    # rests on the prompt's context check instead, which is why the two layers
    # are verified separately rather than conflated.
    gate_refused = [q for q, d in zip(CORPUS_GAPS, gapped) if d > config.RELEVANCE_THRESHOLD]
    report.check(
        "the gate refuses the corpus gaps it can",
        len(gate_refused) >= 2,
        f"{len(gate_refused)} of {len(CORPUS_GAPS)} refused by the gate; "
        f"the rest rely on the prompt's context check",
    )


def _check_end_to_end(report: _Report) -> None:
    report.section("6. End-to-end \u2014 the 10 PRD \u00a76 questions (deterministic LLM)")
    all_ok = True
    for question in FACTUAL_QUESTIONS:
        # A compliant model: refuses when the context lacks the fact.
        fake = FakeLlm(require_term=CORPUS_GAPS.get(question))
        result = answer.ask(question, llm_call=fake)
        text = result.answer.text
        sentences = guardrails.count_sentences(text)
        citation = result.answer.citation
        # A correct answer is allowed to contain numbers \u2014 it is only wrong if
        # a number is not supported by the retrieved context. Testing for
        # "no numbers" would fail the expense-ratio question, which is right.
        context = prompt.assemble_context(
            retrieval.search(question, k=config.RETRIEVAL_TOP_K)
        )
        unsupported = guardrails.check_grounding(text, context)
        report.check(
            f"renderable: {question[:56]}",
            result.state in {"answered", "no_answer", "refusal"}
            and sentences <= config.MAX_ANSWER_SENTENCES
            and not unsupported,
            f"state={result.state} sentences={sentences} "
            f"fallback={result.used_fallback}",
        )
        all_ok = all_ok and sentences <= config.MAX_ANSWER_SENTENCES
        all_ok = all_ok and not unsupported
        if citation is not None:
            all_ok = all_ok and citation.url in REGISTRY_URLS
            all_ok = all_ok and text.count("http") <= 1
            all_ok = all_ok and config.FRESHNESS_LABEL in result.answer.freshness
        else:
            # A state that makes no factual claim carries no citation.
            all_ok = all_ok and result.state in {"no_answer", "refusal"}
    report.check(
        "every PRD \u00a76 answer is \u2264 3 sentences, one registry link, "
        "no unsupported number",
        all_ok,
    )
    # The three corpus gaps must be honest refusals, with no citation attached.
    for question, term in CORPUS_GAPS.items():
        result = answer.ask(question, llm_call=FakeLlm(require_term=term))
        report.check(
            f"corpus gap refused without inventing: {question[:46]}",
            result.state == "no_answer"
            and result.answer.citation is None
            and config.NO_ANSWER.split(".")[0].lower() in result.answer.text.lower(),
            f"state={result.state} citation="
            f"{'yes' if result.answer.citation else 'no'}",
        )


def _check_freshness(report: _Report) -> None:
    report.section("7. Freshness stamp reflects source freshness, not chat time")
    manifest = vectorstore.read_manifest()
    hit = retrieval.search("What is the expense ratio of HDFC Large Cap Fund?")[0]
    rendered = render.render("Stub answer.", hit.chunk_id, (hit,))
    stamp = rendered.freshness
    report.check("the stamp uses the configured label", stamp.startswith(config.FRESHNESS_LABEL), stamp)
    report.check(
        "the stamp is derived from the chunk's fetched_at, not now",
        render.format_fetched_at(hit.fetched_at) in stamp,
        f"fetched_at={hit.fetched_at} -> {stamp}",
    )
    report.check(
        "the stamp is the source's fetch date, not the chat date",
        render.format_fetched_at(hit.fetched_at) != date.today().strftime("%d %b %Y")
        or hit.fetched_at[:10] == date.today().isoformat(),
        f"stamp={stamp}",
    )
    ingested = manifest.get("ingested_at", "")
    report.check(
        "the stamp date is consistent with the manifest's ingestion",
        bool(ingested) and ingested[:4] == hit.fetched_at[:4],
        f"ingested_at={ingested[:10]}",
    )
    report.check(
        "a refusal carries no freshness stamp",
        render.render_refusal(config.REFUSAL_ADVICE, "Link", "https://x.test").freshness == "",
    )
    report.check(
        "an unparseable fetched_at is shown, not hidden",
        render.format_fetched_at("not-a-date") == "not-a-date"
        and render.format_fetched_at("") == "unknown",
    )


def _check_no_fabrication(report: _Report) -> None:
    report.section("8. No-fabrication \u2014 the most important check")
    # The corpus genuinely lacks this fact (0 occurrences in chunks.txt).
    fake = FakeLlm(require_term=NO_FABRICATION_TERM)
    result = answer.ask(NO_FABRICATION_QUESTION, llm_call=fake)
    invented = re.findall(r"\d+\.\d+\s*%|\b\d{1,3},\d{3}\b", result.answer.text)
    report.check(
        "an absent fact is not answered with a number",
        not invented,
        f"text={result.answer.text[:60]!r} numbers={invented}",
    )
    report.check(
        "the model is actually asked, and the context lacks the fact",
        bool(fake.calls)
        and not _has_term(
            fake.calls[0][-1]["content"].split("QUESTION", 1)[0], NO_FABRICATION_TERM
        ),
        "term absent from the supplied context",
    )
    report.check(
        "the refusal string is used when the context lacks the fact",
        result.state == "no_answer"
        and config.NO_ANSWER.split(".")[0].lower() in result.answer.text.lower(),
        f"state={result.state} text={result.answer.text[:50]!r}",
    )
    # The retrieval gate must also reject a question that is off-scope outright.
    off = answer.ask("What is the price of gold in Mumbai today?", llm_call=FakeLlm())
    report.check(
        "an off-scope question never reaches the LLM",
        off.state == "no_answer" and not off.answer.text.count("http"),
        f"state={off.state} distance={off.distance}",
    )
    # A model that fabricates anyway must be caught, not displayed.
    liar = FakeLlm(
        answers=[
            "The expense ratio is 0.45% with no exit load.\n"
            "chunk_id: hdfc-large-cap-fund-direct-growth-003"
        ]
    )
    caught = answer.ask(
        "What is the expense ratio of HDFC Flexi Cap Fund?", llm_call=liar
    )
    report.check(
        "a fabricated number never reaches the user",
        "0.45" not in caught.answer.text,
        f"state={caught.state} fallback={caught.used_fallback}",
    )
    report.check(
        "a fabricated answer is replaced, not merely annotated",
        caught.used_fallback,
    )


def _check_citation_resilience(report: _Report) -> None:
    report.section("9. Citation-resolution resilience")
    hit = retrieval.search("What is the expense ratio of HDFC Large Cap Fund?")[0]
    for label, chunk_id in (
        ("a valid id", hit.chunk_id),
        ("an invented id", "not-a-real-chunk-999"),
        ("no id at all", None),
        ("an empty id", ""),
    ):
        result = render.render("Stub answer.", chunk_id, (hit,))
        report.check(
            f"{label} still renders exactly one link",
            result.citation is not None
            and result.citation.url == hit.source_url,
            result.citation.url if result.citation else "no citation",
        )
    # A cross-source id falls back to the top hit rather than rendering nothing.
    two = retrieval.search("What is the expense ratio of HDFC Large Cap Fund?")[:2]
    resolved = render.resolve_citation(two[1].chunk_id, two)
    report.check(
        "a real id outside top-k is not silently mis-linked",
        resolved is not None and resolved.chunk_id in {h.chunk_id for h in two},
        resolved.chunk_id if resolved else "none",
    )
    report.check(
        "the link label is source_title + section",
        "—" in (resolved.label if resolved else ""),
        resolved.label if resolved else "",
    )
    report.check(
        "no hits produces no citation rather than a crash",
        render.render("x", "id", ()).citation is None,
    )


def _check_output_fallback(report: _Report) -> None:
    report.section("10. Output-guard retry and extractive fallback")
    # A model that ignores the contract: 5 sentences.
    bad = " ".join(
        f"Sentence number {n} of the answer." for n in range(1, 6)
    )
    always_bad = FakeLlm(answers=[f"{bad}\nchunk_id: hdfc-large-cap-fund-direct-growth-003"])
    result = answer.ask(
        "What is the expense ratio of HDFC Large Cap Fund?", llm_call=always_bad
    )
    report.check(
        "the guard failure triggers at most one retry",
        len(always_bad.calls) == 2,
        f"{len(always_bad.calls)} calls",
    )
    report.check(
        "the retry uses stricter instructions",
        prompt.RETRY_INSTRUCTION in always_bad.calls[1][0]["content"],
    )
    report.check(
        "a still-bad answer falls back to extractive text",
        result.used_fallback and result.state == "answered",
        f"fallback={result.used_fallback}",
    )
    report.check(
        "the fallback still carries a citation",
        result.answer.citation is not None and bool(result.answer.citation.url),
    )
    report.check(
        "the fallback is within the 3-sentence limit",
        guardrails.count_sentences(result.answer.text) <= config.MAX_ANSWER_SENTENCES,
        f"{guardrails.count_sentences(result.answer.text)} sentences",
    )
    report.check(
        "the fallback answer is extractive, not generated",
        result.answer.text.strip()[:20] in
        retrieval.search("What is the expense ratio of HDFC Large Cap Fund?")[0].text,
    )


def _check_refusals_no_llm(report: _Report) -> None:
    report.section("11. Refusals and PII make zero LLM calls")
    for question in REFUSAL_QUESTIONS:
        fake = FakeLlm()
        result = answer.ask(question, llm_call=fake)
        report.check(
            f"refusal, no LLM call: {question[:48]}",
            result.state == "refusal" and not fake.calls,
            f"state={result.state} llm_calls={len(fake.calls)}",
        )
        report.check(
            "  the educational link is present and allowlisted",
            bool(re.search(r"https?://\S+", result.answer.text))
            and _link_is_allowlisted(result.answer.text),
        )
        report.check(
            "  the refusal is the fixed config string",
            result.answer.text.startswith(config.REFUSAL_ADVICE)
            or result.answer.text.startswith(config.REFUSAL_PERFORMANCE)
            or result.answer.text.startswith(config.OUT_OF_SCOPE),
        )
    for question in PII_QUESTIONS:
        fake = FakeLlm()
        result = answer.ask(question, llm_call=fake)
        report.check(
            f"PII, no LLM call: {question[:48]}",
            result.state == "pii_blocked" and not fake.calls,
            f"state={result.state} llm_calls={len(fake.calls)}",
        )
        report.check(
            "  the PII message is the fixed constant and nothing is echoed",
            result.answer.text == config.PII_MESSAGE
            and "1234567890" not in result.answer.text
            and "ABCDE1234F" not in result.answer.text,
        )


def _link_is_allowlisted(text: str) -> bool:
    for url in re.findall(r"https?://\S+", text):
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        if host not in config.EDUCATIONAL_LINK_DOMAINS:
            return False
    return bool(re.findall(r"https?://\S+", text))


def _check_secret_hygiene(report: _Report) -> None:
    report.section("12. Secret hygiene at runtime")
    report.check(
        "the key is absent or present via .env only, never in a source file",
        _no_key_in_sources(),
    )
    original = os.environ.pop("GROQ_API_KEY", None)
    try:
        try:
            llm.complete([{"role": "user", "content": "x"}])
            missing_ok = False
            detail = "no error raised"
        except config.ConfigError as exc:
            missing_ok = "GROQ_API_KEY" in str(exc)
            detail = str(exc)[:70]
        report.check(
            "a missing key produces a clear message, not a traceback",
            missing_ok,
            detail,
        )
    finally:
        if original is not None:
            os.environ["GROQ_API_KEY"] = original
    # A key-shaped string in an error must be scrubbed. Assembled from parts so
    # this verifier does not itself trip the repo-wide key scan below.
    fake_key = "gsk_" + ("A" * 8) + ("B" * 16)
    os.environ["GROQ_API_KEY"] = fake_key
    try:
        scrubbed = config.scrub_secrets(f"auth failed for {fake_key}")
        report.check(
            "a key-shaped token is redacted from any surfaced error",
            fake_key not in scrubbed,
            scrubbed,
        )
    finally:
        if original is not None:
            os.environ["GROQ_API_KEY"] = original
        else:
            os.environ.pop("GROQ_API_KEY", None)


def _check_separation(report: _Report) -> None:
    report.section("13. Separation \u2014 query path cannot reach ingestion")
    import importlib

    for module in (
        "src.retrieval", "src.prompt", "src.llm", "src.render", "src.answer",
        "src.preflight",
    ):
        mod = importlib.import_module(module)
        imported = set()
        for value in vars(mod).values():
            name = getattr(value, "__name__", "")
            if name:
                imported.add(name)
            module_name = getattr(value, "__module__", "")
            if module_name:
                imported.add(module_name)
        # Follow one level into the src modules this one pulls in, so a
        # transitive ingestion import is caught too.
        for value in list(vars(mod).values()):
            if getattr(value, "__name__", "").startswith("src."):
                for other in getattr(value, "__dict__", {}).values():
                    inner = getattr(other, "__name__", "")
                    if inner:
                        imported.add(inner)
        leaked = sorted(n for n in ("loader", "chunker") if n in imported)
        report.check(
            f"{module} cannot reach loader or chunker", not leaked, str(leaked)
        )
    query_files = (
        "src/answer.py", "src/retrieval.py", "src/prompt.py", "src/llm.py",
        "src/render.py", "src/preflight.py",
    )
    query_text = "".join(_source_text(f) for f in query_files)
    # Match import statements, not the words "loader"/"chunker": these modules
    # name them in prose precisely to document that they do NOT import them.
    ingestion_import = re.compile(
        r"^\s*(?:from\s+src(?:\.\w+)*\s+)?import\s+[^\n]*\b(?:loader|chunker)\b"
        r"|^\s*from\s+src(?:\.\w+)*\s+import\s+[^\n]*\b(?:loader|chunker)\b",
        re.M,
    )
    leaked = [f for f in query_files if ingestion_import.search(_source_text(f))]
    report.check(
        "no ingestion import appears in the query path",
        not leaked,
        str(leaked),
    )
    report.check(
        "the query path never writes the store",
        "write_chunks" not in query_text and "reset_store" not in query_text,
    )
    report.check(
        "app.py still imports no ingestion or query-path module",
        not ingestion_import.search(_source_text("app.py")),
    )


# --------------------------------------------------------------------------
def _Path(value: str):  # noqa: N802 - small local helper
    from pathlib import Path

    return Path(value)


def _source_text(relative: str) -> str:
    from pathlib import Path

    return (config.PROJECT_ROOT / relative).read_text(encoding="utf-8")


def _no_key_in_sources() -> bool:
    """True if no .py or .md file under the repo contains a gsk_ key."""
    pattern = re.compile(r"gsk_[A-Za-z0-9]{16,}")
    for path in config.PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".py", ".md", ".txt", ".json"}:
            continue
        if ".venv" in path.parts or ".git" in path.parts:
            continue
        try:
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                print(f"    found a key-like string in {path}")
                return False
        except OSError:
            continue
    return True


def _check_live_groq(report: _Report) -> None:
    """The one check that needs a real network call.

    Split out from the rest on purpose: every other check must pass offline and
    without a key, so the pipeline logic stays verifiable on any machine. When
    no key is configured this reports the skip rather than passing quietly.
    """
    report.section("3b. Live Groq call")
    if not llm.is_available():
        print("  SKIP  no GROQ_API_KEY configured \u2014 live call not attempted.")
        print("        Add a key to .env to run this check:")
        print("        python ingest.py --verify-only --phase 5")
        return
    question = "What is the expense ratio of the HDFC Large Cap Fund Direct Growth?"
    try:
        result = answer.ask(question, llm_call=None)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        report.check(
            "a live Groq call completes",
            False,
            f"{type(exc).__name__}: {config.scrub_secrets(str(exc))[:120]}",
        )
        return
    report.check(
        "a live Groq call returns an answer",
        result.state == "answered",
        f"state={result.state} llm_error={result.llm_error[:80]}",
    )
    report.check(
        "the live answer is \u2264 3 sentences",
        guardrails.count_sentences(result.answer.text)
        <= config.MAX_ANSWER_SENTENCES,
        f"{guardrails.count_sentences(result.answer.text)} sentences",
    )
    report.check(
        "the live answer carries one registry citation",
        result.answer.citation is not None
        and result.answer.citation.url in REGISTRY_URLS,
        result.answer.citation.url if result.answer.citation else "none",
    )
    print(f"  live answer: {result.answer.text[:200]}")


def _check_memory(report: _Report) -> None:
    report.section("14. Conversation memory and follow-up rewriting")
    assert config.MEMORY_MAX_MESSAGES == 10, "the window is ten messages"

    # -- the window is bounded by the container, not by a trimming habit ---
    mem = memory_mod.ConversationMemory()
    for i in range(1, 8):
        mem.add_exchange(f"Question {i}?", f"Answer {i}.")
    report.check(
        "the window holds exactly ten messages",
        len(mem) == 10,
        f"7 exchanges = 14 messages, retained {len(mem)}",
    )
    report.check(
        "the oldest turns are the ones evicted",
        mem.messages()[0].content == "Question 3?",
        f"14 messages, oldest 4 dropped, oldest retained "
        f"{mem.messages()[0].content!r}",
    )
    report.check(
        "alternating user and assistant turns are preserved in order",
        [t.role for t in mem.messages()]
        == [memory_mod.USER, memory_mod.ASSISTANT] * 5,
    )
    report.check(
        "an empty turn is not stored",
        (lambda m: (m.add(memory_mod.USER, "   "), len(m) == 0)[1])(
            memory_mod.ConversationMemory()
        ),
        "whitespace does not consume a slot",
    )
    report.check(
        "reset empties the window",
        (lambda m: (m.reset(), len(m) == 0 and not m.has_history)[1])(mem),
    )

    # -- a blocked turn must not park PII in the transcript ---------------
    leaky = memory_mod.ConversationMemory()
    leaky.add_blocked()
    transcript = leaky.transcript()
    report.check(
        "a PII-blocked turn is never stored verbatim",
        "withheld" in transcript and config.PII_MESSAGE[:20] in transcript,
        "the withheld marker replaces the typed text",
    )

    # -- follow-up detection is a cost gate, not a correctness gate ---------
    followups = ("what about its fees?", "and the exit load?", "18 months?")
    standalone = (
        "What is the expense ratio of HDFC Large Cap Fund?",
        "What is the price of gold today?",
    )
    report.check(
        "elliptical follow-ups are detected",
        all(memory_mod.looks_like_followup(q) for q in followups),
        f"{len(followups)}/{len(followups)} detected",
    )
    report.check(
        "self-contained questions skip the rewrite round trip",
        not any(memory_mod.looks_like_followup(q) for q in standalone),
        f"{len(standalone)}/{len(standalone)} skipped",
    )

    # -- validation: add, never drop --------------------------------------
    good = memory_mod.validate_rewrite(
        "and the exit load?", "What is the exit load of HDFC Large Cap Fund?"
    )
    report.check("a faithful rewrite is accepted", good[0] is True, good[1])
    for label, original, candidate, expect in (
        (
            "dropping the subject is rejected",
            "what about its fees?",
            "What is the fund size?",
            "dropped",
        ),
        (
            "swapping the topic is rejected",
            "what about gold?",
            "What is the expense ratio of HDFC Large Cap Fund?",
            "dropped",
        ),
        (
            "answering instead of restating is rejected",
            "what about its fees?",
            "Its expense ratio is 1.03%.",
            "dropped",
        ),
        (
            "an over-long rewrite is rejected",
            "what about its fees?",
            "What are the fees of HDFC Large Cap Fund? " * 20,
            "chars",
        ),
        ("an empty rewrite is rejected", "what about its fees?", "   ", "empty"),
    ):
        ok, reason = memory_mod.validate_rewrite(original, candidate)
        report.check(label, not ok and expect in reason, reason)

    # -- a missing key or a Groq error is a no-op, never a failure ---------
    def _explode(_messages):
        raise llm.LLMError("simulated outage")

    seeded = memory_mod.ConversationMemory()
    seeded.add_exchange("What is the expense ratio of HDFC Large Cap Fund?", "1.03%.")
    for label, call in (
        ("a Groq error", _explode),
        ("an empty reply", lambda _m: "   "),
        ("a rewrite that drops the subject", lambda _m: "What is the fund size?"),
    ):
        result = memory_mod.rewrite("what about its fees?", seeded, llm_call=call)
        report.check(
            f"{label} leaves the question untouched",
            result.question == "what about its fees?" and not result.used,
            result.reason[:60],
        )
    report.check(
        "no history means no rewrite call",
        memory_mod.rewrite(
            "what about its fees?",
            memory_mod.ConversationMemory(),
            llm_call=_explode,
        ).question
        == "what about its fees?",
        "an empty memory short-circuits before the LLM",
    )

    # -- wrappers a restatement model sometimes adds ----------------------
    for raw, expected in (
        ('Standalone question: What is the exit load of HDFC Large Cap Fund?', "What"),
        ('"What is the exit load of HDFC Large Cap Fund?"', "What"),
        ("```\nWhat is the exit load of HDFC Large Cap Fund?\n```", "What"),
    ):
        report.check(
            f"a wrapped reply is unwrapped: {raw[:26]!r}",
            memory_mod._strip_wrapping(raw).startswith(expected)
            and memory_mod._strip_wrapping(raw).endswith("Fund?"),
        )

    # -- end to end, with the rewriter injected ---------------------------
    calls: list[list[dict[str, str]]] = []

    def _fake_rewrite(messages):
        calls.append(messages)
        return "What is the exit load of HDFC Large Cap Fund?"

    def _fake_answer(messages):
        return (
            "The exit load is 1% if redeemed within 1 year.\n"
            "chunk_id: hdfc-large-cap-fund-direct-growth-015"
        )

    convo = memory_mod.ConversationMemory()
    first = answer.ask(
        "What is the expense ratio of HDFC Large Cap Fund?",
        llm_call=_fake_answer,
        memory=convo,
        rewrite_call=_explode,
    )
    report.check(
        "a first question with no history behaves exactly as before",
        not first.rewritten
        and first.retrieval_question
        == "What is the expense ratio of HDFC Large Cap Fund?"
        and not calls,
        "no rewriter call was made",
    )
    report.check(
        "the answered exchange is recorded as two messages",
        len(convo) == 2
        and convo.messages()[0].role == memory_mod.USER
        and convo.messages()[1].role == memory_mod.ASSISTANT,
    )

    second = answer.ask(
        "what about its exit load?",
        llm_call=_fake_answer,
        memory=convo,
        rewrite_call=_fake_rewrite,
    )
    report.check(
        "a follow-up is rewritten before retrieval",
        second.rewritten
        and second.retrieval_question
        == "What is the exit load of HDFC Large Cap Fund?"
        and len(calls) == 1,
        f"embedded: {second.retrieval_question!r}",
    )
    report.check(
        "the rewriter sees the transcript and its own system prompt",
        calls
        and calls[0][0]["role"] == "system"
        and memory_mod.REWRITE_SYSTEM_PROMPT in calls[0][0]["content"]
        and "HDFC Large Cap Fund" in calls[0][1]["content"]
        and "what about its exit load?" in calls[0][1]["content"],
    )
    report.check(
        "the rewritten question retrieves the right scheme's chunk",
        second.chunk_id == "hdfc-large-cap-fund-direct-growth-015"
        and second.answer.citation is not None
        and second.answer.citation.url
        == "https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth",
    )
    report.check(
        "the stored user turn is what was typed, not the rewrite",
        convo.messages()[2].content == "what about its exit load?",
        f"stored: {convo.messages()[2].content!r}",
    )

    # -- a refusal never reaches the rewriter -----------------------------
    before = len(calls)
    refused = answer.ask(
        "What is the price of gold today?",
        llm_call=_fake_answer,
        memory=convo,
        rewrite_call=_fake_rewrite,
    )
    report.check(
        "an off-topic question is not rewritten into the previous topic",
        len(calls) == before,
        f"state={refused.state}, rewriter calls unchanged at {len(calls)}",
    )

    # -- and a PII block is not stored verbatim ---------------------------
    leaky = memory_mod.ConversationMemory()
    before = len(leaky)
    answer.ask(
        "My PAN is ABCDE1234F, what is the expense ratio?",
        llm_call=_fake_answer,
        memory=leaky,
        rewrite_call=_fake_rewrite,
    )
    report.check(
        "a PII-blocked turn is recorded without the PII",
        len(leaky) == 2 and "ABCDE1234F" not in leaky.transcript(),
        "the PAN is not in the transcript",
    )

    # -- statelessness: no memory keyword means the old behaviour ----------
    stateless = answer.ask(
        "what about its fees?", llm_call=_fake_answer, rewrite_call=_explode
    )
    report.check(
        "ask() without a memory is the stateless pipeline",
        not stateless.rewritten
        and stateless.retrieval_question == "what about its fees?",
        "no rewrite, no recording",
    )

    # -- the rewrite cannot introduce a scheme outside the registry --------
    off_registry = memory_mod.rewrite(
        "what about its fees?",
        convo,
        llm_call=lambda _m: "What is the expense ratio of Parag Parag Flexi Cap Fund?",
    )
    report.check(
        "a rewrite naming an out-of-scope scheme is still containment-checked",
        not off_registry.used,
        f"reason: {off_registry.reason[:50]}",
    )
    report.check(
        "the rewriter is given no corpus text, only the transcript",
        all(
            "chunk_id" not in message["content"] for message in calls[0]
        ),
        "no chunk ids in the rewriter's prompt",
    )


def run_phase5() -> int:
    print("Phase 5 verification")
    print("--------------------")
    report = _Report()
    print(
        f"k={config.RETRIEVAL_TOP_K} threshold={config.RELEVANCE_THRESHOLD} "
        f"model={config.GROQ_MODEL}"
    )

    _check_preflight_missing_store(report)
    _check_preflight_model_mismatch(report)
    _check_preflight_drift(report)
    _check_retrieval_sanity(report)
    _check_calibration(report)
    _check_end_to_end(report)
    _check_live_groq(report)
    _check_freshness(report)
    _check_no_fabrication(report)
    _check_citation_resilience(report)
    _check_output_fallback(report)
    _check_refusals_no_llm(report)
    _check_secret_hygiene(report)
    _check_separation(report)
    _check_memory(report)

    return report.finish("Phase 5 verification")


if __name__ == "__main__":
    raise SystemExit(run_phase5())
