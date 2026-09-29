"""C18 — central configuration for every path, model setting, and fixed string.

Both pipelines import this module, so the ingestion and query paths cannot drift
apart on paths, model identity, or user-facing wording.

Secrets: the Groq key is never stored here and never has a literal in source.
It is read from the environment, which is populated from the git-ignored `.env`
file. `require_groq_api_key()` is the only accessor and it reports the variable
by name, never by value.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# Populates os.environ from .env. A missing .env is not an error at import time:
# each entry point reports the specific missing variable when it actually needs
# it. Existing environment variables win, so platform-provided values (e.g. on
# Render) are not overwritten by a local file.
load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    """A missing or invalid setting.

    The message is user-facing and must never contain a secret value.
    """


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
DOC_DIR: Path = PROJECT_ROOT / "doc"
DATA_DIR: Path = PROJECT_ROOT / "data"
CHROMA_DIR: Path = DATA_DIR / "chroma"
CHUNKS_TXT: Path = DATA_DIR / "chunks.txt"
MANIFEST_JSON: Path = DATA_DIR / "manifest.json"

# --------------------------------------------------------------------------
# Chunking (doc/chunking-strategy.md — every value below is justified there)
# --------------------------------------------------------------------------
# Measured on the five in-scope pages (Phase 2, 2026-09-28): 755 label:value
# units, median 13 chars, p90 32, p99 106, max 174; longest visible line 384
# chars. Units are small, discrete facts rather than continuous prose, which is
# why the strategy is one fact per chunk with no overlap.
CHUNK_TARGET_CHARS: int = 320
CHUNK_MAX_CHARS: int = 700
#: Overlap is 0 for the normal path: units are discrete facts, and duplicating a
#: fact across chunks risks two chunks carrying the same figure under different
#: section names. Overlap applies only where a single unit is too long to be one
#: chunk and must be split on a sentence boundary, so no sentence is ever lost.
CHUNK_OVERLAP_SENTENCES: int = 1
#: A unit shorter than this is a fragment (`%`, `--`, a stray bracket) rather than
#: a fact, and is dropped. The measured median unit is 13 chars, so this is set
#: well below the median on purpose: short facts are the common case, not junk.
MIN_UNIT_CHARS: int = 8

#: Line that marks the end of the core region, immediately before the footer.
FOOTER_ANCHOR: str = "Home"

#: Header badges. The lock-in badge comes first on the ELSS page and the risk
#: badge comes after the category tokens, so the two header rules anchor on
#: these separately.
RISK_BADGE = re.compile(r"^(Very High|High|Moderate|Low) Risk$", re.I)
LOCKIN_BADGE = re.compile(r"Lock-in", re.I)

#: The first key-stat label. Bounds the span holding the returns ticker and the
#: NAV quote, which sit between the risk badge and this label.
FIRST_KEY_STAT = re.compile(r"^Min\. for SIP$", re.I)

#: A full date line, e.g. "08 May 2015" — the effective date of an exit-load
#: entry, which is joined to the entry text that follows it.
FULL_DATE = re.compile(r"^\d{1,2} [A-Z][a-z]{2} \d{4}$")

#: The pages render an absent exit load as a bare em-dash pair, which means
#: nothing in an answer. It is rendered as this instead of being deleted, so the
#: fact "there is no exit load" stays answerable and the date is kept.
NIL_VALUE = "--"
NIL_RENDERING = "Nil (no exit load)"

#: The About summary repeats the live NAV inside a prose paragraph. The NAV is
#: excluded everywhere else, so it is removed here too, by dropping the sentence
#: that carries it. Dropping the whole paragraph would lose the launch date and
#: the current fund manager, which are answerable facts.
DROP_SENTENCE_PATTERNS: tuple[str, ...] = (
    r"(?i)\bthe latest nav\b",
)

#: Blocks removed before chunking, as (name, start_pattern, end_pattern).
#: The start match is dropped and the end match is kept, so the following section
#: survives. Order follows the order the blocks appear on the page.
DROP_REGIONS: tuple[tuple[str, str, str], ...] = (
    ("return_calculator_and_historic_returns", r"^Return calculator\b",
     r"^Holdings \("),
    ("holdings_table", r"^Holdings \(", r"^See All\b"),
    ("returns_and_rankings", r"^Returns and rankings\b", r"^Understand terms\b"),
    ("compare_similar_funds", r"^Compare similar funds\b", r"^Fund management\b"),
    # Fund manager cards. Not one of the fact types the brief or the PRD asks
    # for, and each card's "also manages these schemes" list is the only place
    # the other HDFC schemes are named, which invites citing a scheme that is
    # not in the corpus. The current manager is still named in the About summary
    # sentence, so no answerable fact is lost.
    ("fund_management", r"^Fund management$", r"^About "),
    # AMC contact details, up to but not including Custodian. This span also
    # contains "Launch Date", whose value duplicates "Date of Incorporation".
    ("fund_house_contact", r"^Phone$", r"^Custodian$"),
    # RTA contact details, which run to the end of the core region. The end
    # pattern cannot match, so the span is dropped as a tail.
    ("rta_contact", r"^Email$", r"^\Z"),
)

#: Whole lines dropped wherever they appear (navigation and link-only text).
DROP_LINE_PATTERNS: tuple[str, ...] = (
    r"^Check past data$",
    r"^View details$",
    r"^See All$",
    r"^Compare$",
    # Stray duplicate of the date already inside the stamp-duty line.
    r"^from July 1st,? 2020$",
)

#: Fields dropped by label after label/value joining. PRD §4 puts rankings and
#: distributor ratings out of scope. NAV is a live price that would contradict
#: the freshness stamp within a day, so it is not stored either.
DROP_FIELD_PATTERNS: tuple[str, ...] = (
    r"^NAV\b",
    r"^Rating$",
    r"^Rank\b",
    r"^Fund returns$",
    r"^Category average\b",
)

#: Field labels observed on the five pages. A line matching one of these is
#: joined with the line that follows it, so a fact and its label stay together
#: even when the value is alphabetic (`Custodian: HDFC Bank`) and cannot be
#: recognised as a value by shape alone.
LABEL_VOCABULARY: tuple[str, ...] = (
    "Min. for 1st investment",
    "Min. for 2nd investment",
    "Min. for SIP",
    "Fund size (AUM)",
    "Expense ratio",
    "NAV",
    "Rating",
    "Fund house",
    "Custodian",
    "Registrar & Transfer Agent",
    "Total AUM",
    "Rank (total assets)",
    "Date of Incorporation",
    "Launch Date",
    "Investment Objective",
    "Fund benchmark",
    "Scheme Information Document(SID)",
    "Phone",
    "E-mail",
    "Email",
    "Website",
    "Address",
    "Education",
    "Experience",
    "Stamp duty on investment",
    "Tax implication",
    # Glossary terms shown in the "Understand terms" panel.
    "Annualised returns",
    "Absolute returns",
    "Tax",
    "Exit load",
    "Stamp duty",
)

#: Headings recognised as section labels. A matching line sets the section for
#: the units that follow and is not itself emitted as chunk body. "Exit Load" is
#: handled separately in the loader because the same text is also a glossary
#: term: it is a heading only when a dated entry follows it.
SECTION_PATTERNS: tuple[str, ...] = (
    r"^Minimum investments$",
    r"^Understand terms$",
    r"^Exit load, stamp duty and tax$",
    r"^Tax implication$",
    r"^Fund management$",
    r"^About\b",
)

#: Section name substituted for the `About {scheme}` heading.
ABOUT_SECTION: str = "About"

#: Section for units before the first recognised heading: the header badges and
#: the key-stat block.
DEFAULT_SECTION: str = "Key facts"

# --------------------------------------------------------------------------
# Embedding (architecture §10.2 — pinned once, used identically by both paths)
# --------------------------------------------------------------------------
EMBEDDING_MODEL_ID: str = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIMENSION: int = 384
EMBEDDING_NORMALIZE: bool = True
#: MiniLM is not an instruction model, so no prefix is prepended on either path.
EMBEDDING_PROMPT_PREFIX: str = ""
#: Pinned so ingestion and query-time embeddings are produced on the same device
#: with the same kernels. CPU is deliberate: it is what this project was verified
#: on, and a GPU would introduce device-dependent float variation.
EMBEDDING_DEVICE: str = "cpu"
#: Seeded for reproducibility. The encoder is deterministic regardless, but
#: seeding removes any chance of library-level nondeterminism.
EMBEDDING_SEED: int = 0
EMBEDDING_BATCH_SIZE: int = 32
#: Tolerance for the architecture §10.4 round-trip check. Re-embedding stored text
#: can differ in the last float bits because of batching, so the comparison is
#: by max absolute element difference, not exact equality.
ROUND_TRIP_TOLERANCE: float = 1e-5
ROUND_TRIP_SAMPLE_SIZE: int = 12

# --------------------------------------------------------------------------
# Vector store (architecture §2.6, §10.2)
# --------------------------------------------------------------------------
COLLECTION_NAME: str = "mf_faq"
DISTANCE_METRIC: str = "cosine"
#: Bumped when the on-disk schema changes in a way an older build cannot read.
MANIFEST_SCHEMA_VERSION: int = 1
#: Records written per upsert batch. Kept well under 64: this machine's
#: hnswlib backend segfaults on a single upsert carrying ~65 or more records, so
#: batching small is what keeps the write path alive. 144 chunks means 5 batches.
STORE_BATCH_SIZE: int = 32

# --------------------------------------------------------------------------
# Retrieval parameters — calibrated in Phase 5, verification steps 4-5, and
# frozen here. Not guessed.
#
# Observed top-1 cosine distance (k=5, all-MiniLM-L6-v2, normalized) for the
# PRD §6 questions and deliberately out-of-scope questions:
#
#   8 answerable PRD questions .............. 0.0637 - 0.3702
#   2 PRD questions whose fact is NOT in the
#     corpus (capital-gains download, tax
#     reporting documents) ................... 0.5430 - 0.6358
#   8 out-of-scope questions ................. 0.6960 - 0.8455
#
# The three bands are disjoint, so one threshold separates all three. 0.45 is
# the midpoint of the gap between the first and second bands (0.3702, 0.5430).
#
# Note on the middle band: two of the ten PRD §6 questions ask for facts these
# five scheme pages simply do not carry. A threshold low enough to force an
# answer for them would have to sit above 0.6358, which is close enough to the
# off-scope floor (0.6960) that paraphrase sensitivity would start admitting
# genuinely unrelated questions. Refusing them is the correct outcome, not a
# tuning failure: an honest "not in my sources" is what SC-4 and the no-
# fabrication requirement ask for.
#
#   k = 5 because the fact-bearing chunk is at rank 2 for the Flexi Cap SIP
#   minimum and rank 4 for the Large Cap lump-sum minimum; k=3 misses the
#   latter entirely. `assemble_context` then feeds the model only the chunks
#   inside the threshold band, so a larger k does not widen the context.
# --------------------------------------------------------------------------
RETRIEVAL_TOP_K: int | None = 5
RELEVANCE_THRESHOLD: float | None = 0.45

_TUNING_NOTE = "calibrated in Phase 5 from observed scores"


# --------------------------------------------------------------------------
# Conversation memory (C16) — bounded, per-conversation, never persisted.
# --------------------------------------------------------------------------
#: Ten messages, i.e. five question/answer exchanges. The window is short on
#: purpose: a follow-up only resolves "its" or "that fund" against the exchange
#: immediately before it, and a longer window both costs tokens and increases
#: the chance the rewriter anchors on a stale scheme and answers about the
#: wrong fund.
MEMORY_MAX_MESSAGES: int = 10

#: Off by default in the sense that a memory must still be *passed in* — memory
#: is per-conversation state, so there is no module-level singleton to leak one
#: user's history into another's answer. `ask()` without a `memory` keyword is
#: byte-for-byte the stateless pipeline it was before memory existed.
MEMORY_REWRITE_ENABLED: bool = True

#: Skip the rewrite round trip unless the question actually looks like a
#: follow-up. "What is the expense ratio of HDFC Large Cap Fund?" is
#: self-contained and must not pay for a second Groq call; "what about its
#: fees?" cannot be embedded without one. Latency, not correctness, is the
#: reason: the rewrite is the only step that runs on questions that already
#: have a working retrieval path.
MEMORY_REWRITE_MIN_WORDS: int = 3

#: Ceiling on a rewritten question. The rewriter may expand "its" into a scheme
#: name, never into a paragraph; anything longer is a model that has started
#: answering the question instead of restating it, and is discarded.
MEMORY_MAX_QUESTION_CHARS: int = 400

#: The rewriter gets a small budget of its own. It is a restatement task, not a
#: reasoning task, so it should not be able to spend the answer's budget.
MEMORY_REWRITE_MAX_TOKENS: int = 200



# --------------------------------------------------------------------------
# Fixed user-facing strings — constants, never generated by the LLM.
# --------------------------------------------------------------------------
DISCLAIMER: str = "Facts-only. No investment advice."

WELCOME_LINE: str = (
    "Ask a factual question about an HDFC Mutual Fund scheme and I will answer "
    "from the official source pages."
)

#: The three example questions shown on load (PRD §10.2), drawn from the
#: question types named in doc/ProblemStatement.txt.
EXAMPLE_QUESTIONS: tuple[str, ...] = (
    "What is the expense ratio of the HDFC Large Cap Fund?",
    "What is the lock-in period for the HDFC ELSS Tax Saver Fund?",
    "How do I download my capital-gains statement?",
)

REFUSAL_ADVICE: str = (
    "I only share facts from the official source pages, so I cannot advise on "
    "whether to buy, sell, or hold a scheme. You can read the scheme's own "
    "details and risk disclosures on the official page, and speak to a registered "
    "mutual fund distributor if you would like a view on suitability."
)

REFUSAL_PERFORMANCE: str = (
    "I do not calculate, compare, or quote returns. For performance figures, "
    "please refer to the official factsheet published by the AMC."
)

OUT_OF_SCOPE: str = (
    "I only cover the HDFC Mutual Fund schemes in my sources: HDFC Large Cap, "
    "HDFC Equity (Flexi Cap), HDFC ELSS Tax Saver, HDFC Small Cap, and HDFC "
    "Balanced Advantage. I cannot answer questions about other funds or other "
    "asset classes."
)

PII_MESSAGE: str = (
    "For your security, please do not share personal identifiers such as PAN, "
    "Aadhaar, account numbers, OTPs, email addresses, or phone numbers. I do "
    "not store them. Please ask your question again without personal details."
)

#: Stands in for a blocked input, in the conversation buffer and in the drawn
#: thread alike. One constant, because the two must not drift: the thread is what
#: the user can see and copy, so if it showed the raw text while the buffer
#: withheld it, the value would still have been displayed and stored in
#: `st.session_state` (PRD §10.7, SC-8).
PII_WITHHELD: str = "[withheld: blocked at the input screen]"

NO_ANSWER: str = (
    "I do not have that in my sources. I can answer questions about expense "
    "ratio, exit load, minimum SIP and investment amounts, ELSS lock-in, "
    "riskometer, benchmark, and how to download statements and tax documents, "
    "for the five HDFC Mutual Fund schemes in my sources."
)

#: Educational links shown with refusals. Curated fixed constants, never
#: LLM-generated (architecture §5.1, PRD SC-6). Every entry is a stable,
#: public, top-level page. No deep path is invented: the factsheet is reached by
#: following "Other documents" / "Downloads" from the AMC homepage, and a
#: guessed deep link would rot silently.
EDUCATIONAL_LINKS: dict[str, str] = {
    # Where the official factsheets are published (PRD §9.9).
    "factsheet": "https://www.hdfcmf.com/",
    # The AMC's own scheme pages: objective, benchmark, riskometer, disclosures.
    "scheme_details": "https://www.hdfcmf.com/",
    # Regulator material on mutual fund risk and investor education.
    "risk_and_regulation": "https://www.sebi.gov.in/",
    # Industry body: investor education and complaint redressal.
    "investor_education": "https://www.amfiindia.com/",
    # The corpus source pages themselves.
    "source_pages": "https://groww.in/",
}

#: Domains the educational links are allowed to point at. Verification asserts
#: every configured link is on this list, so a link cannot be quietly replaced
#: with something invented or non-public.
EDUCATIONAL_LINK_DOMAINS: frozenset[str] = frozenset({
    "hdfcmf.com",
    "sebi.gov.in",
    "amfiindia.com",
    "groww.in",
})

# --------------------------------------------------------------------------
# Guardrail patterns (Phase 4 — architecture §5)
# --------------------------------------------------------------------------
# Every pattern below is deliberately narrow. A guardrail that blocks an
# ordinary fee or period question is a defect, not caution: the 10 PRD §6
# factual questions must all pass, and verification feeds them through
# `classify_intent` to prove it.

#: A PAN is five letters, four digits, one letter. Case-sensitive on purpose: a
#: lowercased match would fire on ordinary words, and no real PAN is lowercase.
PII_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")

#: Aadhaar is twelve digits, optionally spaced in 4-4-4 groups.
PII_AADHAAR = re.compile(r"(?<!\d)\d{4}[ -]?\d{4}[ -]?\d{4}(?!\d)")

#: A long run of digits is an account/folio number, not a fee. Fund pages quote
#: fees, percentages, and short year counts, never bare runs this long.
PII_LONG_DIGIT_RUN = re.compile(r"(?<!\d)\d{8,}(?!\d)")

#: A 10-digit run, which is also the shape of an Indian mobile number. Checked
#: before the 8+ run so the reported category is the more specific one.
PII_PHONE = re.compile(r"(?<!\d)(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)")

PII_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

#: Keyword-based PII. An OTP is identified by what it is called, not by shape.
PII_KEYWORDS: tuple[str, ...] = (
    r"\botp\b",
    r"\bone[-\s]?time\s+(?:password|pin|code)\b",
    r"\bverification\s+code\b",
    r"\bmy\s+pan\b",
    r"\bpan\s*(?:number|no\.?|#|is)\b",
    r"\baadhaar\b",
    r"\baccount\s+number\b",
    r"\bfolio\s+number\b",
)

#: Order matters: the most specific category is reported first.
PII_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pan", PII_PAN),
    ("aadhaar", PII_AADHAAR),
    ("phone", PII_PHONE),
    ("long_digit_run", PII_LONG_DIGIT_RUN),
    ("email", PII_EMAIL),
    ("keyword", re.compile("|".join(PII_KEYWORDS), re.I)),
)

#: Opinion verbs and suitability language (architecture §5.1). "Which of these
#: ... should be in my portfolio" and "is X safe for a 3-year goal" are advice
#: questions even without the word "recommend", so suitability phrasing counts.
INTENT_ADVICE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I) for p in (
        r"\bshould\s+(?:i|we|one)\b",
        r"\bshould\s+(?:i\s+)?(?:buy|sell|hold|invest|add|exit|switch|allocate)\b",
        r"\b(?:buy|sell|purchase|redeem|invest\s+in|subscribe)\b",
        r"\bgood\s+time\b",
        r"\bbest\s+(?:fund|scheme|option|choice|pick)\b",
        r"\brecommend\w*\b",
        r"\badvice\b",
        r"\bopinion\b",
        r"\bworth\s+(?:it|investing)\b",
        r"\bsafe\s+for\b",
        r"\bsuitable\s+for\b",
        r"\bportfolio\b",
        r"\ballocat\w*\b",
        r"\basset\s+allocation\b",
        r"\bwhich\s+of\s+these\b",
    )
)

#: Return and ranking language (PRD SC-7). Checked before advice so "which fund
#: has the best returns" reports the performance refusal, which is the more
#: specific and more useful of the two.
INTENT_PERFORMANCE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I) for p in (
        r"\breturns?\b",
        r"\bperformance\b",
        r"\bperform\w*\b",
        r"\boutperform\w*\b",
        r"\bbest\s+(?:perform\w*|return\w*)\b",
        r"\bcompare\b",
        r"\bversus\b",
        r"\bvs\.?\b",
        r"\bwhich\s+(?:fund|scheme|one)\b.{0,30}\bbetter\b",
        r"\b(?:cagr|xirr|irr|nav)\b",
        r"\bwill\s+(?:i|it|this|the\s+\w+)\s+(?:get|make|earn|return)\b",
        r"\bhow\s+much\s+(?:can|will|should)\s+i\s+(?:earn|get|make)\b",
    )
)

#: Other AMCs. Naming a fund house that is not in the registry is out of scope
#: (architecture §5.2). "Equity" is deliberately absent: two in-scope schemes are
#: equity funds, so the word cannot imply out-of-scope.
OUT_OF_SCHEME_AMCS: tuple[str, ...] = (
    r"parag parikh", r"moti[la]l", r"\buti\b", r"axis", r"kotak", r"\bsbi\b",
    r"nippon", r"\blic\b", r"tata", r"icici", r"birla", r"mirae", r"\bdsp\b",
    r"canara", r"\bidfc\b", r"sundaram", r"bandhan", r"pgim", r"invesco",
    r"franklin", r"aditya", r"sbi\s+bluechip", r"axis\s+large", r"quant\s+mfs",
    r"jm\s+financial", r"navi\s+mutual", r"pragati", r"groww\s+asset",
)

#: Other asset classes and non-mutual-fund topics.
OFF_TOPIC_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I) for p in (
        r"\bcrypto\w*\b", r"\bbitcoin\b", r"\bblockchain\b",
        r"\breal\s+estate\b", r"\bstock\s+picks?\b", r"\bshares?\b",
        r"\bppf\b", r"\bepfo\b", r"\bfixed\s+deposit\b", r"\bsavings\s+account\b",
        r"\bpost\s+office\b", r"\brented\b", r"\bnse\s+trading\b",
        r"\bforex\b", r"\bgold\s+(?:etf|price)\b",
    )
)

#: Recommendation phrasing banned from generated answers (architecture §5.1).
#: This is the second line of defence behind the system prompt.
OUTPUT_ADVICE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I) for p in (
        r"\byou\s+should\b",
        r"\bwe\s+recommend\b",
        r"\bi\s+recommend\w*\b",
        r"\bi\s+would\s+suggest\b",
        r"\bis\s+a\s+good\s+(?:buy|investment|pick|choice)\b",
        r"\bconsider\s+investing\b",
        r"\bworth\s+investing\b",
        r"\b(?:ideal|perfect|suitable|right)\s+for\s+you\b",
        r"\bmust\s+invest\b",
        r"\ballocate\s+\d+\s*%",
    )
)

#: Return figures and rankings banned from generated answers (PRD SC-7). A
#: percentage is only a violation when a return word sits next to it, so a
#: legitimate "Expense ratio: 1.03%" or a glossary definition of "annualised
#: returns" is not caught.
OUTPUT_PERFORMANCE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I) for p in (
        r"\d+(?:\.\d+)?\s*%\s*(?:per\s*(?:year|annum)|p\.?a\.?|cagr|returns?)\b",
        r"\b(?:best|worst|top|highest|lowest)[-\s]perform\w*\b",
        r"\boutperform\w*\b",
        r"\brank(?:ed|ing|ings)?\s+(?:by|amongst?)\b",
        r"\bcompare[ds]?\b.{0,40}\b(?:fund|scheme|return|performance)\b",
        r"\bwhich\s+(?:fund|scheme|one)\b.{0,30}\bbetter\b",
        r"\bbeat\s+the\s+(?:index|benchmark|fund)\b",
    )
)

#: A bare URL in an answer means the model wrote a link itself. Citations are
#: resolved in application code from stored metadata (architecture §4), so a
#: URL in generated text is always a fabrication risk.
OUTPUT_URL = re.compile(r"https?://\S+", re.I)

#: Maximum sentences in an answer (PRD §8, §10).
MAX_ANSWER_SENTENCES: int = 3

# --------------------------------------------------------------------------
# Groq (query time only)
# --------------------------------------------------------------------------
#: Chat model used for answer generation. 70b is chosen over the 8b variant
#: because the output contract (three sentences, a verbatim-only number rule, and
#: a trailing `chunk_id:` line) is an instruction-following task, and a contract
#: violation costs a whole extra round trip.
#: `openai/gpt-oss-120b` replaced the decommissioned `llama-3.3-70b-versatile`,
#: which now returns HTTP 404 `model_not_found`. Overridable via the
#: `GROQ_MODEL` environment variable, so a rotation is a `.env` edit, not a code
#: change. The output contract is an instruction-following task, and a contract
#: violation costs a whole extra round trip.
GROQ_MODEL: str = "openai/gpt-oss-120b"

#: Low temperature on purpose: this is extraction and rephrasing of supplied
#: context, not creative writing. Lower is more literal about copying numbers.
GROQ_TEMPERATURE: float = 0.1

#: The cap has to cover the *reasoning* as well as the answer. `gpt-oss-120b`
#: is a reasoning model and Groq bills its `reasoning` tokens against the same
#: `max_tokens` budget, so the 300 this used to allow was spent before the final
#: channel started: measured completion_tokens of 173 for a trivial single-fact
#: lookup but 300 (finish_reason="length", empty content) for anything needing
#: two numbers compared. 1200 leaves room for a few thousand characters of
#: deliberation plus the three-sentence answer and the chunk_id line, and the
#: sentence guard, not this cap, is what bounds a runaway generation.
GROQ_MAX_TOKENS: int = 1200

#: Seconds. Architecture §9 budgets ~1s per query, so a hang must fail fast into
#: the extractive fallback rather than leave the user waiting.
GROQ_TIMEOUT: float = 20.0

FRESHNESS_LABEL: str = "Last updated from sources:"


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------
def require_groq_api_key() -> str:
    """Return the Groq API key, or raise ConfigError naming the variable.

    The key's value is never logged, printed, or embedded in the error message.
    """
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        raise ConfigError(
            "GROQ_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return api_key


def has_groq_api_key() -> bool:
    """Return True if a non-empty key is available, without exposing it."""
    return bool(os.environ.get("GROQ_API_KEY", "").strip())


def scrub_secrets(text: str) -> str:
    """Redact anything that looks like the API key from a message.

    Used on every third-party error string before it is surfaced. SDK exceptions
    are not expected to echo the key, but "not expected" is not a guarantee, and
    an error message is exactly the kind of string that ends up in a log.
    """
    key = os.environ.get("GROQ_API_KEY", "").strip()
    cleaned = text or ""
    if key:
        cleaned = cleaned.replace(key, "***")
    return re.sub(r"\bgsk_[A-Za-z0-9]{8,}\b", "***", cleaned)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def require_tuning() -> None:
    """Fail loudly if Phase 5 calibration values are still unset."""
    missing = [
        name
        for name, value in (
            ("RETRIEVAL_TOP_K", RETRIEVAL_TOP_K),
            ("RELEVANCE_THRESHOLD", RELEVANCE_THRESHOLD),
        )
        if value is None
    ]
    if missing:
        raise ConfigError(
            f"Retrieval settings {', '.join(missing)} are {_TUNING_NOTE}."
        )


def validate_ingestion_config() -> list[str]:
    """Return a list of ingestion-config problems; empty means valid.

    The Groq key is deliberately not checked here: offline ingestion never calls
    the LLM and must not require an API key.
    """
    problems: list[str] = []
    if not EMBEDDING_MODEL_ID:
        problems.append("EMBEDDING_MODEL_ID is empty.")
    if EMBEDDING_DIMENSION <= 0:
        problems.append("EMBEDDING_DIMENSION must be positive.")
    if DISTANCE_METRIC != "cosine":
        problems.append(
            f"DISTANCE_METRIC must be 'cosine' (got {DISTANCE_METRIC!r}); "
            "it is fixed at collection creation and must match at query time."
        )
    if not DATA_DIR.is_dir() and not DATA_DIR.parent.is_dir():
        problems.append(f"DATA_DIR parent does not exist: {DATA_DIR.parent}")
    return problems


def describe_settings() -> dict[str, object]:
    """Return non-secret settings for display and verification.

    Contains no API key or any other secret, by construction.
    """
    return {
        "project_root": str(PROJECT_ROOT),
        "doc_dir": str(DOC_DIR),
        "data_dir": str(DATA_DIR),
        "chroma_dir": str(CHROMA_DIR),
        "chunks_txt": str(CHUNKS_TXT),
        "manifest_json": str(MANIFEST_JSON),
        "embedding_model_id": EMBEDDING_MODEL_ID,
        "embedding_dimension": EMBEDDING_DIMENSION,
        "embedding_normalize": EMBEDDING_NORMALIZE,
        "distance_metric": DISTANCE_METRIC,
        "collection_name": COLLECTION_NAME,
        "retrieval_top_k": RETRIEVAL_TOP_K,
        "relevance_threshold": RELEVANCE_THRESHOLD,
        "groq_api_key_present": has_groq_api_key(),
    }
