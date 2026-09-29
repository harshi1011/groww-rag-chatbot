"""C14 — the facts-only system prompt, context assembly, and output contract.

The prompt is the last place a fabricated fact can be prevented, and it is also
where the citation contract is set up. The model is asked to return a bare
`chunk_id`, never a URL: the URL is resolved in `src.render` from stored
metadata, so a fabricated link is structurally impossible (architecture §4).

Two rules in here are not stylistic. "If the context does not contain the fact,
say so" is what makes refusal possible, and is the single most important
sentence for SC-4. "Never mention a percentage, ratio, or amount unless it
appears verbatim in the context" is belt-and-braces for SC-5, because a model
given a plausible-looking question about a fee will otherwise supply a
plausible-looking fee.
"""

from __future__ import annotations

from src import config

# The system prompt is a config constant, not a literal buried in a function, so
# that Phase 6 can display it and the verification can assert against it.
SYSTEM_PROMPT: str = (
    "You are a facts-only reference assistant for HDFC Mutual Fund schemes.\n"
    "\n"
    "Rules, in priority order:\n"
    "1. Answer ONLY from the CONTEXT given in the user message. The context is "
    "the complete set of facts you have. Treat anything not in it as unknown.\n"
    "2. If the context does not contain the answer, reply with exactly: "
    "\"This information is not in my sources.\" Do not guess, estimate, or "
    "substitute a figure from your own knowledge.\n"
    "3. Quote no number, ratio, or amount unless it appears verbatim in the "
    "context.\n"
    "4. Maximum three sentences. No tables, no lists, no bullet points, no "
    "calculations, no arithmetic.\n"
    "5. Never recommend, suggest, rank, compare, or predict returns. Never say "
    "which scheme is better, safer, or best. No return figures, no "
    "\"returns comparison\", no future performance.\n"
    "6. No URLs, no links, no markdown. Do not describe where the information "
    "came from.\n"
    "7. Answer the question that was asked, and nothing more.\n"
    "\n"
    "Output format, exactly:\n"
    "<the answer, one to three sentences>\n"
    "chunk_id: <the id of the context block the answer came from>"
)

# Appended on the single allowed retry (architecture §5.5). It restates the
# failure modes the output guard just caught rather than adding new ones, so the
# retry fixes a specific violation instead of re-teaching the whole contract.
RETRY_INSTRUCTION: str = (
    "\n\nYour previous answer was rejected. Rewrite it to satisfy these "
    "requirements exactly:\n"
    "- At most three sentences.\n"
    "- No numbers, figures, or amounts that are not copied verbatim from the "
    "CONTEXT.\n"
    "- No advice, no recommendation, no ranking, no return or performance "
    "claim, no future prediction.\n"
    "- No URL and no link.\n"
    "- If the CONTEXT does not contain the answer, reply with exactly: "
    "\"This information is not in my sources.\"\n"
    "- End with a line of the form: chunk_id: <one id from the CONTEXT>"
)


def context_block(index: int, hit) -> str:
    """Render one retrieved chunk as a labelled, citable context block."""
    return (
        f"[{index}] chunk_id: {hit.chunk_id}\n"
        f"scheme: {hit.scheme_name}\n"
        f"section: {hit.section}\n"
        f"text: {hit.text}\n"
    )


def assemble_context(hits, max_chunks: int | None = None) -> str:
    """Concatenate retrieved chunks into the context the model is allowed to use.

    Only chunks within the relevance gate's distance band are included, and at
    most `max_chunks` of them. Feeding the model chunks the gate already
    rejected would reintroduce exactly the irrelevant context the gate exists to
    keep out.
    """
    if not hits:
        return ""
    if max_chunks is None:
        max_chunks = config.RETRIEVAL_TOP_K or 5
    used = [hit for hit in hits if hit.distance <= config.RELEVANCE_THRESHOLD]
    if not used:
        used = [hits[0]]
    blocks = [context_block(i, hit) for i, hit in enumerate(used[:max_chunks], 1)]
    return "\n".join(blocks)


def build_user_prompt(question: str, hits, max_chunks: int | None = None) -> str:
    """The user turn: the context, then the question, then the contract."""
    return (
        f"CONTEXT\n\n{assemble_context(hits, max_chunks)}\n\n"
        f"QUESTION\n{question}\n\n"
        "Answer the question from the CONTEXT only, in at most three "
        "sentences, then output the supporting chunk_id on its own line."
    )


def build_messages(question: str, hits, retry: bool = False) -> list[dict[str, str]]:
    """The full message list for one Groq call.

    `retry=True` produces the single stricter retry permitted by architecture
    §5.5 before the extractive fallback.
    """
    system = SYSTEM_PROMPT
    user = build_user_prompt(question, hits)
    if retry:
        system = system + RETRY_INSTRUCTION
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


__all__ = [
    "RETRY_INSTRUCTION",
    "SYSTEM_PROMPT",
    "assemble_context",
    "build_messages",
    "build_user_prompt",
    "context_block",
]
