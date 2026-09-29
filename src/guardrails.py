"""C10, C11, C13, C14, C16 — the guardrails, as pure functions.

Architecture §5 orders guardrails by cost so the cheapest deterministic check
runs first and a blocked request never reaches the LLM:

| Order | Guard | Function | On trigger |
|---|---|---|---|
| 1 | PII | `screen_pii` | fixed `PII_MESSAGE`; value never echoed |
| 2 | Intent | `classify_intent` / `screen_input` | fixed refusal + educational link |
| 3 | Relevance | `check_relevance` | fixed `NO_ANSWER` |
| 4 | Grounding | `check_grounding` | fail closed to `NO_ANSWER` |
| 5 | Output | `check_output` | retry once, then `build_fallback_answer` |

Three properties are load-bearing and are asserted by the Phase 4 verification
rather than assumed:

* **Nothing here logs, stores, or echoes the question.** Every result object
  carries a category or a boolean, never the text that triggered it (SC-8).
* **Every refusal string is a constant.** No refusal path generates prose, so a
  refusal cannot drift into advice (architecture §5.1).
* **The intent guard is tuned for false positives, not false negatives.** A bot
  that refuses everything looks safe and is useless, so all 10 PRD §6 factual
  questions must classify as `factual` — verification feeds them through
  `classify_intent` to prove it.

No function here imports the store, the model, or the network. That is what
makes them independently testable and what keeps the query path free of
ingestion concerns (architecture §9).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src import config
from src.config import ConfigError

# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PIIResult:
    """Outcome of the PII screen.

    Carries the *category* of what was found and never the value itself, so a
    caller cannot accidentally echo, log, or persist the sensitive string by
    printing this object.
    """

    blocked: bool
    categories: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.blocked


@dataclass(frozen=True)
class IntentDecision:
    """Outcome of intent classification.

    `category` is one of `factual`, `advice`, `performance`, `out-of-scheme`,
    `off-topic`. `refused` is True for every category except `factual`, and then
    `message` and `link` are populated from config constants.
    """

    category: str
    refused: bool
    message: str = ""
    link_label: str = ""
    link_url: str = ""
    #: The patterns that fired, for debugging only. Never user-facing text.
    matched: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class InputDecision:
    """Combined PII + intent verdict for one raw question."""

    ok: bool
    intent: IntentDecision
    pii: PIIResult
    message: str = ""
    link_label: str = ""
    link_url: str = ""


@dataclass(frozen=True)
class OutputResult:
    """Outcome of the output guard (C16). `ok` True means the answer may render."""

    ok: bool
    problems: tuple[str, ...] = ()
    sentence_count: int = 0
    citation: str = ""


# --------------------------------------------------------------------------
# C10 — PII
# --------------------------------------------------------------------------
def screen_pii(text: str) -> PIIResult:
    """Screen raw user input for personal identifiers.

    Runs on the raw question, before any embedding, so a question containing a
    PAN is never converted into a vector and never leaves the process
    (architecture §5.3). Checks every rule rather than stopping at the first
    match, so a question carrying both an email and a phone is fully described
    in the category list.
    """
    if not text:
        return PIIResult(blocked=False)

    categories = tuple(
        name for name, pattern in config.PII_RULES if pattern.search(text)
    )
    return PIIResult(blocked=bool(categories), categories=categories)


# --------------------------------------------------------------------------
# C11 — Intent
# --------------------------------------------------------------------------
#: Words that carry no intent signal and would otherwise make every question
#: look like it matched something.
_ADVICE_STOPWORDS: frozenset[str] = frozenset({
    "i", "is", "the", "a", "of", "in", "for", "to", "my", "and", "it", "this",
    "that", "can", "me", "you", "with", "are", "on", "or", "be", "do",
})


def _first_match(
    text: str, patterns: tuple[re.Pattern[str], ...]
) -> tuple[str, ...]:
    """Return the source of each matching pattern, for the debug field."""
    return tuple(p.pattern for p in patterns if p.search(text))


def classify_intent(text: str) -> IntentDecision:
    """Classify a question as factual or a refusal category.

    Order is deliberate. Performance is checked before advice because "which
    fund has the best returns" trips both vocabularies and the performance
    refusal is the more specific and more useful response. Out-of-scheme and
    off-topic are checked before advice so a question naming another AMC gets
    the scope message rather than a suitability refusal.
    """
    lowered = text.lower()

    if _matches_any(lowered, config.INTENT_PERFORMANCE_PATTERNS):
        return IntentDecision(
            category="performance",
            refused=True,
            message=config.REFUSAL_PERFORMANCE,
            link_label="Official factsheets",
            link_url=config.EDUCATIONAL_LINKS["factsheet"],
            matched=_first_match(text, config.INTENT_PERFORMANCE_PATTERNS),
        )

    if _matches_amc(lowered):
        return IntentDecision(
            category="out-of-scheme",
            refused=True,
            message=config.OUT_OF_SCOPE,
            link_label="Schemes in my sources",
            link_url=config.EDUCATIONAL_LINKS["source_pages"],
            matched=("amc",),
        )

    if _matches_any(lowered, config.OFF_TOPIC_PATTERNS):
        return IntentDecision(
            category="off-topic",
            refused=True,
            message=config.OUT_OF_SCOPE,
            link_label="Investor education",
            link_url=config.EDUCATIONAL_LINKS["investor_education"],
            matched=_first_match(text, config.OFF_TOPIC_PATTERNS),
        )

    if _matches_any(lowered, config.INTENT_ADVICE_PATTERNS):
        return IntentDecision(
            category="advice",
            refused=True,
            message=config.REFUSAL_ADVICE,
            link_label="Scheme details and risk disclosures",
            link_url=config.EDUCATIONAL_LINKS["scheme_details"],
            matched=_first_match(text, config.INTENT_ADVICE_PATTERNS),
        )

    return IntentDecision(category="factual", refused=False)


def _matches_any(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _matches_amc(text: str) -> bool:
    """True if the text names a mutual fund house outside the registry."""
    return any(
        re.search(pattern, text, re.I) for pattern in config.OUT_OF_SCHEME_AMCS
    )


# --------------------------------------------------------------------------
# The one entry point the query path uses
# --------------------------------------------------------------------------
def screen_input(text: str) -> InputDecision:
    """Run the PII screen then the intent guard, in cost order.

    Returns an `InputDecision` whose `ok` False means: do not embed, do not
    retrieve, do not call the LLM, and render `message` instead. This is the
    single function Phase 5 calls before anything expensive happens.
    """
    pii = screen_pii(text)
    if pii.blocked:
        # No link, and no reason: naming a category would tell an attacker what
        # the screen caught.
        return InputDecision(
            ok=False,
            intent=IntentDecision(category="pii", refused=True),
            pii=pii,
            message=config.PII_MESSAGE,
        )

    intent = classify_intent(text)
    if intent.refused:
        return InputDecision(
            ok=False,
            intent=intent,
            pii=pii,
            message=intent.message,
            link_label=intent.link_label,
            link_url=intent.link_url,
        )

    return InputDecision(ok=True, intent=intent, pii=pii)


# --------------------------------------------------------------------------
# C13 — Relevance gate
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RelevanceResult:
    """Whether retrieved context clears the relevance threshold."""

    relevant: bool
    best_score: float | None
    threshold: float
    reason: str = ""


def check_relevance(
    scores: list[float] | None, threshold: float | None = None
) -> RelevanceResult:
    """Gate retrieval on the top-1 cosine distance (architecture §5, order 3).

    Cosine *distance* is used, so smaller is better: 0.0 is identical and 2.0 is
    opposite. `None` is handled explicitly — an empty result set is "not
    relevant", never "assume relevance".

    The threshold is a placeholder until Phase 5 calibrates it from the observed
    score distribution. A caller that supplies neither an argument nor a
    calibrated config value gets a loud error rather than a silent guess, because
    a guessed threshold decides what the bot claims not to know.
    """
    if threshold is None:
        if config.RELEVANCE_THRESHOLD is None:
            raise ConfigError(
                "RELEVANCE_THRESHOLD is not calibrated yet "
                f"({config._TUNING_NOTE}). Pass an explicit threshold to "
                "check_relevance() to exercise the gate before Phase 5."
            )
        threshold = config.RELEVANCE_THRESHOLD

    if not scores:
        return RelevanceResult(
            relevant=False,
            best_score=None,
            threshold=threshold,
            reason="no results returned",
        )

    best = min(scores)
    if best > threshold:
        return RelevanceResult(
            relevant=False,
            best_score=best,
            threshold=threshold,
            reason=f"top-1 distance {best:.4f} exceeds threshold {threshold:.4f}",
        )
    return RelevanceResult(relevant=True, best_score=best, threshold=threshold)


# --------------------------------------------------------------------------
# C14 — Grounding
# --------------------------------------------------------------------------
#: The rule the Phase 5 system prompt must carry. Kept here, next to the check
#: that enforces it, so the instruction and its enforcement cannot drift apart.
GROUNDING_RULES: str = (
    "Answer only from the supplied context. If the context does not contain "
    "the fact, say that it is not in your sources. Never use prior knowledge, "
    "never guess, and never fill a gap with a typical value."
)

#: Numbers in an answer are the fabrication risk that matters: a wrong expense
#: ratio or lock-in period is something a user can act on. Every number in the
#: answer must appear in the supplied context.
_NUMERIC = re.compile(r"\d+(?:\.\d+)?")

#: Tokens that carry no grounding signal of their own.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "in",
    "is", "it", "its", "of", "on", "or", "that", "the", "to", "was", "were",
    "this", "with", "you", "your", "what", "which", "can", "do", "does", "i",
})


def check_grounding(answer: str, context: str) -> list[str]:
    """Return grounding problems; empty means the answer is supported.

    Deterministic support check, no LLM involved. Two conditions:

    1. every number in the answer appears in the context, and
    2. the answer shares at least one meaningful term with the context.

    The first is what makes "fails closed, never parametric knowledge" testable
    without a model. A wrong figure is the failure a user could act on, so it is
    the one that is checked directly.
    """
    problems: list[str] = []
    if not answer.strip():
        return ["answer is empty"]

    lowered_context = context.lower()

    for number in _NUMERIC.findall(answer):
        if number not in context:
            problems.append(f"number {number!r} is not in the context")

    answer_terms = {
        token
        for token in re.findall(r"[a-z]{4,}", answer.lower())
        if token not in _STOPWORDS
    }
    if not any(token in lowered_context for token in answer_terms):
        problems.append("answer shares no meaningful term with the context")

    return problems


# --------------------------------------------------------------------------
# C16 — Output checks
# --------------------------------------------------------------------------
#: A sentence ends at . ! or ? followed by whitespace and a capital, which
#: leaves decimals such as "1.03%" intact.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")


def split_sentences(text: str) -> list[str]:
    """Split into sentences, keeping terminal punctuation."""
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text.strip())]
    return [p for p in parts if p]


def count_sentences(text: str) -> int:
    return len(split_sentences(text))


def check_output(
    answer: str, chunk_id: str | None, valid_chunk_ids: frozenset[str]
) -> OutputResult:
    """Check a generated answer before it may be rendered (architecture §5, order 5).

    Rejects, in order: an empty answer, a missing or unresolvable citation, a
    bare URL in the text, more than three sentences, recommendation phrasing, and
    return or ranking phrasing. Every problem is reported at once so a retry can
    be told everything that is wrong in a single round.
    """
    problems: list[str] = []
    text = (answer or "").strip()

    if not text:
        return OutputResult(ok=False, problems=("answer is empty",))

    if not chunk_id:
        problems.append("no citation chunk_id")
    elif chunk_id not in valid_chunk_ids:
        problems.append(f"citation {chunk_id!r} is not in the store")

    if config.OUTPUT_URL.search(text):
        problems.append("answer contains a URL; citations are resolved in code")

    sentences = count_sentences(text)
    if sentences > config.MAX_ANSWER_SENTENCES:
        problems.append(
            f"{sentences} sentences exceeds the limit of "
            f"{config.MAX_ANSWER_SENTENCES}"
        )

    if _matches_any(text, config.OUTPUT_ADVICE_PATTERNS):
        problems.append("answer contains recommendation phrasing")

    if _matches_any(text, config.OUTPUT_PERFORMANCE_PATTERNS):
        problems.append("answer contains return or ranking phrasing")

    return OutputResult(
        ok=not problems,
        problems=tuple(problems),
        sentence_count=sentences,
        citation=chunk_id or "",
    )


# --------------------------------------------------------------------------
# Fallback — verification step 8
# --------------------------------------------------------------------------
def build_fallback_answer(chunk_text: str) -> str:
    """Build the templated extractive answer from the top chunk.

    Extractive by construction: it slices the stored chunk text and joins the
    leading sentences. Nothing is generated, so the fallback cannot introduce a
    fact that is not in the corpus, and it still carries a citation because the
    caller renders the `chunk_id` alongside it (architecture §4).
    """
    sentences = split_sentences(chunk_text or "")
    if not sentences:
        return ""
    return " ".join(sentences[: config.MAX_ANSWER_SENTENCES])


__all__ = [
    "GROUNDING_RULES",
    "InputDecision",
    "IntentDecision",
    "OutputResult",
    "PIIResult",
    "RelevanceResult",
    "build_fallback_answer",
    "check_grounding",
    "check_output",
    "check_relevance",
    "classify_intent",
    "count_sentences",
    "screen_input",
    "screen_pii",
    "split_sentences",
]
