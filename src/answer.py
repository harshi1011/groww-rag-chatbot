"""The query-time pipeline, composed but not rendered (architecture §3).

This is the module Phase 6's UI will call, and the module the Phase 5
verification drives. It exists as a separate composition root so the full path
question → embed → retrieve → gate → prompt → LLM → output guard → render is
testable now, with no Streamlit anywhere in it.

The order of operations is the guardrail order from architecture §5, and every
early return below it is a state the UI can draw directly:

    pii  -> refuse, never touches the store
    intent -> refusal + educational link, never touches the LLM
    relevance -> "not in my sources" + nearest topics, never touches the LLM
    llm + output guard -> retry once, then extractive fallback
    -> one citation, one freshness stamp

The LLM is injected rather than imported at call time (`llm_call`). That is not
only for testing: it is what makes "refusals make no LLM call" a structural
property — there is no code path from a refusal to a client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from src import config, guardrails, llm, memory as memory_mod, prompt, render, retrieval

#: One Groq round trip. Injected so verification can substitute a deterministic
#: generator and so a refusal can be proven to never invoke it.
LlmCall = Callable[[list[dict[str, str]]], str]

UNKNOWN_IN_SOURCES = "This information is not in my sources."


@dataclass(frozen=True)
class QueryResult:
    """A renderable outcome plus the state label the UI branches on."""

    state: str
    answer: render.RenderedAnswer
    chunk_id: str | None = None
    attempts: int = 0
    used_fallback: bool = False
    distance: float | None = None
    problems: tuple[str, ...] = field(default_factory=tuple)
    llm_error: str = ""
    #: What was actually embedded. Equals `question` unless memory rewrote it,
    #: so a UI can show "interpreted as ..." and a test can assert the rewrite
    #: happened without re-deriving it.
    retrieval_question: str = ""
    #: True when `retrieval_question` differs from what the user typed.
    rewritten: bool = False
    #: Why the rewriter declined, when it did. Diagnostic, not user-facing.
    rewrite_reason: str = ""


def ask(
    question: str,
    llm_call: LlmCall | None = None,
    k: int | None = None,
    threshold: float | None = None,
    memory: memory_mod.ConversationMemory | None = None,
    rewrite_call=None,
    record: bool = True,
) -> QueryResult:
    """Answer one question end to end and return a renderable result.

    `memory` is optional and defaults to None, which is exactly the stateless
    pipeline: no rewriter call, no recording, and byte-identical behaviour to
    before memory existed. Memory is passed per conversation rather than held in
    a module global, so one user's history cannot reach another user's answer.
    """
    decision = guardrails.screen_input(question)

    # `InputDecision.ok` is False for a refusal as well as for a PII block, so
    # the PII result is checked directly. Collapsing the two would return the
    # PII message for an advice question, which is the wrong state and the
    # wrong link.
    if decision.pii.blocked:
        if memory is not None and record:
            memory.add_blocked()
        return QueryResult(
            state="pii_blocked",
            answer=render.render_pii_blocked(),
            retrieval_question=question,
        )

    if decision.intent.refused:
        if memory is not None and record:
            memory.add_exchange(question, decision.message)
        return QueryResult(
            state="refusal",
            answer=render.render_refusal(
                decision.message, decision.link_label, decision.link_url
            ),
            retrieval_question=question,
        )

    # The rewrite happens after the input guard and before retrieval, which is
    # the only place it can be useful: it exists to change what gets embedded,
    # and it is a second LLM call, so it must not run on a question that is
    # already going to be refused or blocked.
    resolved = memory_mod.rewrite(question, memory, llm_call=rewrite_call)
    search_text = resolved.question

    found = retrieval.retrieve(search_text, k=k, threshold=threshold)

    if not found.relevant:
        rendered = render.render_no_answer(found.hits)
        if memory is not None and record:
            memory.add_exchange(question, rendered.text)
        return QueryResult(
            state="no_answer",
            answer=rendered,
            distance=found.best_distance,
            retrieval_question=search_text,
            rewritten=resolved.used,
            rewrite_reason=resolved.reason,
        )

    if llm_call is None:
        llm_call = llm.complete

    valid_ids = frozenset(hit.chunk_id for hit in found.hits)
    context = prompt.assemble_context(found.hits)

    # At most two generations: the answer, then one stricter retry. The retry
    # exists because the guard is stricter than the model; the extractive
    # fallback exists because the retry is not guaranteed to help (SC-4).
    last_problems: tuple[str, ...] = ()
    raw = ""
    for attempt in (1, 2):
        try:
            raw = llm_call(prompt.build_messages(search_text, found.hits, retry=attempt == 2))
        except (llm.LLMError, config.ConfigError) as exc:
            if memory is not None and record:
                memory.add_exchange(question, _fallback_text(found.hits))
            return QueryResult(
                state="llm_unavailable",
                answer=render.RenderedAnswer(
                    text=f"{_fallback_text(found.hits)}",
                    citation=None,
                    fallback_used=True,
                ),
                used_fallback=True,
                distance=found.best_distance,
                llm_error=str(exc),
                retrieval_question=search_text,
                rewritten=resolved.used,
                rewrite_reason=resolved.reason,
            )

        answer_text = llm.strip_chunk_id(raw)
        chunk_id = llm.extract_chunk_id(raw)

        # A decline is handled before the output guard, not after. A model that
        # says "not in my sources" has no supporting chunk, so requiring a
        # citation would fail it and push a compliant refusal into the
        # extractive fallback, which answers a question the model just said it
        # could not answer. That is the worst outcome available here.
        if _is_refusal(answer_text):
            rendered = render.render_no_answer(found.hits)
            if memory is not None and record:
                memory.add_exchange(question, rendered.text)
            return QueryResult(
                state="no_answer",
                answer=rendered,
                attempts=attempt,
                distance=found.best_distance,
                retrieval_question=search_text,
                rewritten=resolved.used,
                rewrite_reason=resolved.reason,
            )

        check = guardrails.check_output(answer_text, chunk_id or "", valid_ids)
        last_problems = tuple(check.problems)

        if check.ok and not guardrails.check_grounding(answer_text, context):
            resolved_answer = render.render(answer_text, chunk_id, found.hits)
            if memory is not None and record:
                memory.add_exchange(question, resolved_answer.text)
            return QueryResult(
                state="answered",
                answer=resolved_answer,
                chunk_id=resolved_answer.citation.chunk_id if resolved_answer.citation else None,
                attempts=attempt,
                distance=found.best_distance,
                retrieval_question=search_text,
                rewritten=resolved.used,
                rewrite_reason=resolved.reason,
            )

    fallback_answer = guardrails.build_fallback_answer(found.hits[0].text)
    resolved_answer = render.render(fallback_answer, found.hits[0].chunk_id, found.hits,
                                    fallback_used=True)
    if memory is not None and record:
        memory.add_exchange(question, resolved_answer.text)
    return QueryResult(
        state="answered",
        answer=resolved_answer,
        chunk_id=found.hits[0].chunk_id,
        attempts=2,
        used_fallback=True,
        distance=found.best_distance,
        problems=last_problems,
        retrieval_question=search_text,
        rewritten=resolved.used,
        rewrite_reason=resolved.reason,
    )


def _is_refusal(answer: str) -> bool:
    """True when the model's answer is the 'not in my sources' decline.

    Matched loosely on purpose: a compliant model may append a chunk_id, vary
    the case, or add a trailing period, and none of that changes the fact that
    it declined to make a claim.
    """
    normalised = " ".join((answer or "").lower().split()).strip(" .\"'")
    return normalised.startswith("this information is not in my sources")


def _fallback_text(hits) -> str:
    """The answer body used when the LLM is unavailable.

    Extractive and capped, so a Groq outage degrades into "here is the source
    text" rather than into an error page or an unsourced guess.
    """
    if not hits:
        return config.NO_ANSWER
    return guardrails.build_fallback_answer(hits[0].text)


__all__ = ["LlmCall", "QueryResult", "UNKNOWN_IN_SOURCES", "ask"]