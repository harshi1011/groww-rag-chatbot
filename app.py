"""Phase 6 — the Streamlit UI, and nothing else.

This file is a formatting layer. It draws what the pipeline already decided and
it does not decide anything itself:

* Every answer, refusal, PII block, and "not in my sources" comes from
  `answer.ask`, which is the only place the guardrails run.
* The citation is `render.Citation`, resolved from store metadata, so the link
  shown here cannot be one the model wrote. This file never builds a URL.
* The freshness line is the backend's string, stamped from the source's
  `fetched_at`. This file never calls `date.today()`; a UI-generated timestamp
  would claim the page was read when the app was merely opened.
* Conversation memory is the `ConversationMemory` from Phase 5, held in
  `st.session_state` and passed straight into `ask`.

Per architecture §9 this entry point must never import the ingestion pipeline:
no fetch, no chunking, and no store-write code path. `preflight` is the one
exception and it is read-only — it checks that the store exists and matches the
embedder before the first question, so a missing artefact is a clear message
rather than a traceback on the user's first keystroke.
"""

from __future__ import annotations

import re

import streamlit as st

from src import answer, config, memory as memory_mod, preflight

st.set_page_config(
    page_title="HDFC Mutual Fund FAQ Assistant",
    layout="centered",
)

#: A backend message may end in `Label: https://...`, which is how
#: `render.render_refusal` attaches its educational link. Splitting it back out
#: lets it be drawn as a real link instead of loose text. Only ever applied to
#: backend-controlled strings (the refusal and PII messages are `config`
#: constants); model output is excluded by the output guard, which rejects any
#: answer containing a bare URL, so this cannot launder a model-written link.
_TRAILING_LINK = re.compile(r"^(?P<label>[^\n:]{3,80}):\s*(?P<url>https?://\S+)$")


def _split_trailing_link(text: str) -> tuple[str, str, str]:
    """Split a message into (body, label, url); label and url are "" if absent."""
    body = (text or "").rstrip()
    lines = body.split("\n")
    if len(lines) < 2:
        return body, "", ""
    match = _TRAILING_LINK.match(lines[-1].strip())
    if not match:
        return body, "", ""
    return "\n".join(lines[:-1]).strip(), match["label"], match["url"]


def _init_state() -> None:
    """One memory per browser session, plus the display transcript."""
    if "memory" not in st.session_state:
        st.session_state.memory = memory_mod.ConversationMemory()
    if "turns" not in st.session_state:
        st.session_state.turns = []
    if "pending" not in st.session_state:
        st.session_state.pending = None


def _draw_result(result: answer.QueryResult) -> None:
    """Draw one assistant turn from a backend result."""
    rendered = result.answer

    # Only when memory actually resolved a reference, so a normal question is
    # not annotated with noise. Transparency about what was embedded, not a
    # second answer.
    if result.rewritten and result.retrieval_question:
        st.caption(f"Follow-up understood as: {result.retrieval_question}")

    body, link_label, link_url = _split_trailing_link(rendered.text)
    if body:
        st.markdown(body)

    if rendered.citation is not None:
        # The required source link. The label says which page section opens, so
        # the reader knows what they are about to click.
        st.markdown(
            f"Source: [{rendered.citation.label}]({rendered.citation.url})"
        )
    elif link_url:
        st.markdown(f"[{link_label}]({link_url})")

    # The required "Last updated from sources:" line. Empty unless a citation
    # was resolved, so an unsourced refusal never carries a freshness stamp.
    if rendered.freshness:
        st.caption(rendered.freshness)

    if rendered.fallback_used:
        st.caption(
            "Shown directly from the source text, because the assistant could "
            "not produce a verified answer this time."
        )

    if result.state == "llm_unavailable":
        st.warning(
            "The assistant is temporarily unable to reach its language model, "
            "so the source text is shown above instead of a written answer."
        )


def _ask(question: str) -> None:
    """Run one question through the pipeline and record it for display.

    A blocked input is recorded as `config.PII_WITHHELD` rather than as the text
    the user typed. The backend already decided this: `result.state` is
    `pii_blocked` only when the guardrail fired, and it is the only signal this
    file needs in order not to display the value. Matching on the question text
    here instead would need a second, weaker copy of the PII patterns, and the
    two would eventually disagree — which is the failure PRD §10.7 forbids. It
    also matters that the *visible* thread is covered: the buffer is in-memory,
    but this transcript is what a user can read, copy, and screenshot.
    """
    question = (question or "").strip()
    if not question:
        return
    result = answer.ask(question, memory=st.session_state.memory)
    shown = config.PII_WITHHELD if result.state == "pii_blocked" else question
    st.session_state.turns.append(("user", shown))
    st.session_state.turns.append(("assistant", result))


def _run_preflight() -> preflight.Preflight:
    """Check the store and the embedder once per session, then cache it."""
    if "preflight" not in st.session_state:
        st.session_state.preflight = preflight.run()
    return st.session_state.preflight


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------
_init_state()

st.title("HDFC Mutual Fund Facts-only FAQ Assistant")
st.write(
    "Ask a factual question about one of the five HDFC Mutual Fund schemes in "
    "my sources, and I will answer from the official source pages only. I cover "
    "published facts such as expense ratio, exit load, minimum investments, "
    "ELSS lock-in, riskometer, and benchmark. I do not give opinions, "
    "recommendations, or return figures."
)
st.info(config.DISCLAIMER)

status = _run_preflight()

if not status.ok:
    st.error(status.message)
    for problem in status.problems[1:]:
        st.text(f"- {problem}")
    st.stop()

if not config.has_groq_api_key():
    st.warning(
        "GROQ_API_KEY is not set, so no written answer can be generated. Copy "
        ".env.example to .env and add your key, then restart the app."
    )
    st.stop()

for warning in status.warnings:
    st.warning(warning)

# --------------------------------------------------------------------------
# Example questions
# --------------------------------------------------------------------------
st.subheader("Example questions")
st.caption("Click one to ask it.")

example_columns = st.columns(len(config.EXAMPLE_QUESTIONS))
for column, example in zip(example_columns, config.EXAMPLE_QUESTIONS):
    if column.button(example, key=f"example::{example}", use_container_width=True):
        st.session_state.pending = example

if st.session_state.turns:
    if st.button("New conversation", key="reset"):
        st.session_state.memory = memory_mod.ConversationMemory()
        st.session_state.turns = []
        st.rerun()

# --------------------------------------------------------------------------
# Transcript
# --------------------------------------------------------------------------
for role, payload in st.session_state.turns:
    with st.chat_message("user" if role == "user" else "assistant"):
        if role == "user":
            st.markdown(payload)
        else:
            _draw_result(payload)

# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------
typed = st.chat_input(
    "Ask a factual question about an HDFC Mutual Fund scheme"
)

# An example click and a typed question are the same action, so they share one
# path. Reading the pending value here, after the buttons have run, is what lets
# a single rerun both record the click and ask the question.
question = typed or st.session_state.pending
if question:
    st.session_state.pending = None
    _ask(question)
    st.rerun()
