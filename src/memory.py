"""C16 — bounded conversation memory, and the follow-up rewriter.

A follow-up like "what about its fees?" is not embeddable. The word "its" carries
no scheme signal, so the same embedding lands on whatever the store finds
nearest and the answer comes back about the wrong fund. This module turns that
follow-up into a standalone question *before* retrieval, using the last ten
messages as the only source of what the pronoun refers to.

Three properties are load-bearing, and each exists because of a specific way this
can go wrong:

1. **The rewrite may add, never drop.** `rewrite()` requires every meaningful
   word of the original question to survive into the rewrite. Without that
   check a rewriter that misreads "and gold?" as a follow-up about the previous
   fund would happily restate it as a fund question, and the pipeline would
   answer a question the user did not ask. The check is containment, not
   similarity: cheap, deterministic, and impossible to argue with.
2. **Any failure is a no-op.** No key, no client, a timeout, an empty reply, a
   reply that is too long, a reply that fails the containment check — every one
   of them returns the original question untouched. Memory is an enhancement, so
   it is never allowed to become a new way for a question to fail.
3. **The rewriter never sees the corpus and never answers.** It is given the
   transcript and asked to restate a question. It cannot introduce a fact,
   because nothing it says is used as evidence — the rewrite only selects which
   chunks are retrieved, and every number in the answer still has to appear
   verbatim in those chunks to pass the output guard.

Nothing here is persisted. A `ConversationMemory` is owned by one conversation
and dropped with it, so no user's history can reach another user's answer.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

from src import config, llm

USER = "user"
ASSISTANT = "assistant"

#: Injected so verification can substitute a deterministic rewriter and prove
#: that a refusal or a PII block never reaches one.
RewriteCall = "callable"


# --------------------------------------------------------------------------
# The rewriter's instructions
# --------------------------------------------------------------------------
#: A restatement task, so it gets its own short system prompt rather than any
#: part of the answer prompt. It is deliberately forbidden from answering: if it
#: starts writing the answer, `MEMORY_MAX_QUESTION_CHARS` will cut it and the
#: containment check will reject it.
REWRITE_SYSTEM_PROMPT: str = (
    "You rewrite a follow-up question into a standalone question.\n"
    "\n"
    "Rules:\n"
    "1. Keep every word of the original question exactly as written. Your only "
    "edit is to INSERT the specific scheme or detail that the reference stands "
    "for. Do not swap one word for a synonym, do not paraphrase, do not "
    "re-order into a different question.\n"
    "2. Resolve pronouns and elliptical references (\"it\", \"its\", \"that "
    "fund\", \"this one\", \"and the exit load?\") using ONLY the conversation "
    "above.\n"
    "3. Do NOT answer the question. Do NOT add facts, figures, or schemes that "
    "the conversation does not already mention.\n"
    "4. If the original question is already standalone, or if the reference "
    "cannot be resolved from the conversation, repeat it back unchanged.\n"
    "5. If the question changes topic (for example from a fund to the price of "
    "gold), keep the new topic. Never pull an unrelated question back onto the "
    "previous subject.\n"
    "\n"
    "Examples, where the conversation was about HDFC Large Cap Fund:\n"
    "\"and the exit load?\" -> \"What is the exit load of HDFC Large Cap Fund?\"\n"
    "\"what about its fees?\" -> \"What are the fees of HDFC Large Cap Fund?\"\n"
    "\"and its benchmark?\" -> \"What is the benchmark of HDFC Large Cap Fund?\"\n"
    "\n"
    "Output: the rewritten question only. No prefix, no quotes, no explanation."
)


#: Words that make a question worth a rewrite round trip. This is a cost gate,
#: not a correctness gate: a false negative costs a worse retrieval, a false
#: positive costs a Groq call, and the containment check in `rewrite()` makes a
#: pointless rewrite harmless anyway.
_FOLLOW_UP_MARKERS = re.compile(
    r"\b("
    r"its|it\b|that|this|those|these|they|them|he|she|"
    r"same|also|another|the same|"
    r"what about|how about|and if|and what|and the|and its|"
    r"why|how much|how many|when|where|which"
    r")\b",
    re.I,
)

#: A word worth protecting across the rewrite. Two letters is too short to be
#: meaningful ("is", "of", "in") and stopwords carry no subject, so both are
#: excluded to avoid demanding that the rewriter echo filler.
_STOPWORDS = frozenset(
    """a an the and or but if of to in on at for from by with about its it is are
    was were be been do does did has have had can could will would shall should
    may might must i we you they he she this that these those my our your their
    as so than then there here what which who whom whose when where why how much
    many me us him her them not no yes please tell give show""".split()
)

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]+")


@dataclass(frozen=True)
class Turn:
    """One stored message."""

    role: str
    content: str

    def as_line(self) -> str:
        return f"{self.role}: {self.content}"


@dataclass
class Rewrite:
    """The outcome of a rewrite attempt, including why it was refused.

    `question` is always safe to embed: it is either a validated rewrite or the
    original. `used` says which, so a caller can label the query in the UI
    instead of guessing from string equality.
    """

    question: str
    used: bool = False
    reason: str = ""


@dataclass
class ConversationMemory:
    """The last `MEMORY_MAX_MESSAGES` messages of one conversation.

    A `deque(maxlen=...)` is the whole design: the bound is enforced by the
    container, so there is no path — however many turns run — that grows memory
    without limit or lets a caller forget to trim it.
    """

    max_messages: int = field(default_factory=lambda: config.MEMORY_MAX_MESSAGES)
    _turns: deque[Turn] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._turns = deque(maxlen=max(1, int(self.max_messages)))

    # -- recording ---------------------------------------------------------
    def add(self, role: str, content: str) -> None:
        """Append one message, evicting the oldest if the window is full.

        Empty content is dropped rather than stored: a blank turn would consume
        one of the ten slots and tell the rewriter nothing.
        """
        text = (content or "").strip()
        if not text:
            return
        self._turns.append(Turn(role=role, content=text))

    def add_exchange(self, question: str, answer: str) -> None:
        """Record one completed question/answer pair as two messages.

        The user's own words are stored, not the rewritten question, so the
        transcript reads back as the conversation the user actually had.
        """
        self.add(USER, question)
        self.add(ASSISTANT, answer)

    def add_blocked(self) -> None:
        """Record that a turn happened without storing what was typed.

        PII is blocked *before* the text is ever used for anything, and that has
        to include the transcript: a PAN or a phone number that was correctly
        refused at the input gate must not be parked in a conversation buffer
        for a later rewrite call to read. The marker is shared with the UI so the
        drawn thread and this buffer cannot disagree about what was kept.
        """
        self.add(USER, config.PII_WITHHELD)
        self.add(ASSISTANT, config.PII_MESSAGE)

    # -- reading -----------------------------------------------------------
    def messages(self) -> tuple[Turn, ...]:
        """The retained window, oldest first."""
        return tuple(self._turns)

    def transcript(self, limit: int | None = None) -> str:
        """The window as text for the rewriter, oldest first."""
        turns = self._turns
        if limit is not None:
            turns = list(turns)[-max(0, limit):]
        return "\n".join(turn.as_line() for turn in turns)

    @property
    def has_history(self) -> bool:
        return bool(self._turns)

    def reset(self) -> None:
        self._turns.clear()

    def __len__(self) -> int:
        return len(self._turns)


# --------------------------------------------------------------------------
# Follow-up detection
# --------------------------------------------------------------------------
def looks_like_followup(question: str) -> bool:
    """True when `question` plausibly refers back to the conversation.

    Cheap and deliberately over-inclusive, because the cost of guessing wrong
    in one direction is a Groq call and in the other is a failed retrieval.
    """
    text = (question or "").strip()
    if not text:
        return False
    if _FOLLOW_UP_MARKERS.search(text):
        return True
    # A very short question is elliptical almost by definition: "and AUM?",
    # "18 months?", "and the benchmark".
    return len(_WORD.findall(text)) <= config.MEMORY_REWRITE_MIN_WORDS


# --------------------------------------------------------------------------
# Validation of a candidate rewrite
# --------------------------------------------------------------------------
def _meaningful_words(text: str) -> set[str]:
    return {
        word.lower()
        for word in _WORD.findall(text or "")
        if len(word) > 2 and word.lower() not in _STOPWORDS
    }


def validate_rewrite(original: str, candidate: str) -> tuple[bool, str]:
    """Check a candidate rewrite is safe to embed. Returns (ok, reason).

    The containment rule is the important one: every meaningful word of the
    original must appear in the candidate, lowercased. That permits adding the
    scheme name behind "its" while forbidding the two failures that matter —
    dropping the user's actual subject, or swapping in a different one.
    """
    if not candidate or not candidate.strip():
        return False, "empty rewrite"

    cleaned = candidate.strip()

    if len(cleaned) > config.MEMORY_MAX_QUESTION_CHARS:
        return False, (
            f"rewrite is {len(cleaned)} chars, over "
            f"{config.MEMORY_MAX_QUESTION_CHARS}: the model is answering, "
            "not restating"
        )

    if "\n" in cleaned:
        return False, "rewrite spans multiple lines"

    original_words = _meaningful_words(original)
    rewritten_words = _meaningful_words(cleaned)
    dropped = original_words - rewritten_words
    if dropped:
        return False, f"rewrite dropped {sorted(dropped)}"

    if len(rewritten_words) > len(original_words) + 12:
        return False, "rewrite added too much to be a restatement"

    return True, "ok"


# --------------------------------------------------------------------------
# The rewrite itself
# --------------------------------------------------------------------------
def build_rewrite_messages(
    question: str, transcript: str
) -> list[dict[str, str]]:
    """The rewriter's messages. Separate from the answer prompt by design."""
    return [
        {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Conversation so far:\n"
                f"{transcript}\n\n"
                "Follow-up question: "
                f"{question}\n\n"
                "Standalone question:"
            ),
        },
    ]


def rewrite(
    question: str,
    memory: ConversationMemory | None,
    llm_call: RewriteCall | None = None,
) -> Rewrite:
    """Return a standalone version of `question` for retrieval.

    Never raises and never returns an unusable question: any failure, including
    a missing API key, a Groq error, or a rewrite that fails validation, comes
    back as the original text with `used=False` and a `reason` explaining why.
    """
    original = (question or "").strip()
    if not original:
        return Rewrite(question=question, reason="empty question")

    if memory is None or not memory.has_history:
        return Rewrite(question=original, reason="no history")

    if not config.MEMORY_REWRITE_ENABLED:
        return Rewrite(question=original, reason="rewrite disabled")

    if not looks_like_followup(original):
        return Rewrite(question=original, reason="question is self-contained")

    if llm_call is None:
        llm_call = llm.complete

    try:
        raw = llm_call(
            build_rewrite_messages(original, memory.transcript())
        )
    except (llm.LLMError, config.ConfigError) as exc:
        return Rewrite(question=original, reason=f"rewrite call failed: {exc}")

    candidate = _strip_wrapping(raw)

    ok, reason = validate_rewrite(original, candidate)
    if not ok:
        return Rewrite(question=original, reason=reason)

    if candidate.lower() == original.lower():
        return Rewrite(question=original, reason="nothing to resolve")

    return Rewrite(question=candidate, used=True, reason="rewritten")


def _strip_wrapping(text: str) -> str:
    """Unwrap a reply that arrived quoted, prefixed, or on a second line.

    A restatement model asked for "the question only" still sometimes answers
    `Standalone question: What is ...`, so the wrappers are removed rather than
    treated as a failed validation. Anything still wrapped after this fails the
    containment check on its own.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return cleaned

    first, _, rest = cleaned.partition("\n")
    if "standalone question" in first.lower():
        if rest.strip():
            cleaned = rest.strip()
        else:
            # The prefix arrived on the same line, e.g. "Standalone question:
            # What is ...". Splitting on the newline alone would miss it.
            _, _, after = first.partition(":")
            cleaned = after.strip() or cleaned

    cleaned = cleaned.strip().strip("`").strip()
    for quote in ('"', "'", "“", "”"):
        if len(cleaned) > 1 and cleaned.startswith(quote) and cleaned.endswith(quote):
            cleaned = cleaned[1:-1].strip()
            break
    return cleaned


__all__ = [
    "ASSISTANT",
    "ConversationMemory",
    "REWRITE_SYSTEM_PROMPT",
    "Rewrite",
    "Turn",
    "USER",
    "build_rewrite_messages",
    "looks_like_followup",
    "rewrite",
    "validate_rewrite",
]
