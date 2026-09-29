"""C17 — renderable answer, exactly one resolved citation, and the freshness stamp.

This module is where a URL is allowed to exist. The model never writes one, so
citation resolution is a lookup in metadata the store already holds
(architecture §4). Three consequences, all of them the point:

* A fabricated link is impossible, not merely discouraged.
* A bad or missing `chunk_id` from the model is survivable: resolution falls back
  to the top-ranked chunk and exactly one link is still rendered (§4.4).
* `Last updated from sources:` is formatted from the chunk's `fetched_at`, so it
  reports when the *source* was read. It never uses the current time, which
  would falsely imply the underlying fact is current today (§4.7).

Phase 6 adds the Streamlit drawing calls. This returns plain data, so the logic
is testable now and the UI stays a formatting layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src import config

_DATE_FORMATS = ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d")


@dataclass(frozen=True)
class Citation:
    """One resolved citation: what to label the link and where it points."""

    chunk_id: str
    label: str
    url: str
    fetched_at: str
    source_title: str
    section: str


@dataclass(frozen=True)
class RenderedAnswer:
    """Everything needed to draw one answer block."""

    text: str
    citation: Citation | None
    freshness: str = ""
    fallback_used: bool = False

    def __str__(self) -> str:
        parts = [self.text]
        if self.citation:
            parts.append(f"Source: [{self.citation.label}]({self.citation.url})")
        if self.freshness:
            parts.append(self.freshness)
        return "\n".join(parts)


def _label(hit) -> str:
    """Link label is `source_title` + `section`, so the reader knows what opens."""
    title = (getattr(hit, "source_title", "") or "").strip()
    section = (getattr(hit, "section", "") or "").strip()
    if title and section:
        return f"{title} — {section}"
    return title or section or "Source"


def format_fetched_at(value: str) -> str:
    """Render an ISO `fetched_at` as `12 Sep 2026`, falling back to the raw value.

    An unparseable timestamp is passed through rather than dropped: an honest
    "unknown date" beats a silently missing freshness line, because a reader who
    cannot date the fact should be able to see that.
    """
    raw = (value or "").strip()
    if not raw:
        return "unknown"
    head = raw.split("T")[0].strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(head, fmt).strftime("%d %b %Y")
        except ValueError:
            continue
    return raw


def resolve_citation(chunk_id: str | None, hits) -> Citation | None:
    """Map the model's `chunk_id` to metadata, else use the top-ranked chunk.

    §4.4: if the model omits or invents an id, fall back to the top-ranked
    chunk's URL. An invented id that happens to be a real chunk is honoured,
    because verifying which chunk the model actually used is not possible here;
    an invented id that matches nothing falls back instead of rendering a link
    to nothing.
    """
    if not hits:
        return None
    chosen = None
    if chunk_id:
        for hit in hits:
            if hit.chunk_id == chunk_id:
                chosen = hit
                break
    if chosen is None:
        chosen = hits[0]
    return Citation(
        chunk_id=chosen.chunk_id,
        label=_label(chosen),
        url=chosen.source_url,
        fetched_at=chosen.fetched_at,
        source_title=chosen.source_title,
        section=chosen.section,
    )


def render(answer: str, chunk_id: str | None, hits, fallback_used: bool = False,
           extra_valid_ids: frozenset[str] | None = None) -> RenderedAnswer:
    """Build the renderable answer: text, one citation, freshness line."""
    citation = resolve_citation(chunk_id, hits)
    if citation is None:
        return RenderedAnswer(
            text=answer, citation=None, fallback_used=fallback_used
        )
    valid = {hit.chunk_id for hit in hits}
    if extra_valid_ids:
        valid |= set(extra_valid_ids)
    return RenderedAnswer(
        text=answer,
        citation=citation,
        freshness=f"{config.FRESHNESS_LABEL} {format_fetched_at(citation.fetched_at)}",
        fallback_used=fallback_used,
    )


def render_no_answer(hits=()) -> RenderedAnswer:
    """The honest no-answer state (architecture §5.4), with no citation claim.

    Nearest available topics are suggested so the user is not simply stonewalled,
    but no source link is attached: there is no retrieved fact to support one.
    """
    topics: list[str] = []
    for hit in hits[:3]:
        topic = (getattr(hit, "scheme_name", "") or "").strip()
        if topic and topic not in topics:
            topics.append(topic)
    text = config.NO_ANSWER
    if topics:
        text = f"{text} The closest topics in my sources are: {', '.join(topics)}."
    return RenderedAnswer(text=text, citation=None)


def render_refusal(message: str, link_label: str | None, link_url: str | None) -> RenderedAnswer:
    """A refusal plus its educational link, and no freshness stamp.

    The link is a fixed educational resource, not a retrieved fact, so giving it
    a "Last updated" line would be a small lie about a page the app never read.
    """
    text = message
    if link_label and link_url:
        text = f"{message}\n{link_label}: {link_url}"
    return RenderedAnswer(text=text, citation=None)


def render_pii_blocked() -> RenderedAnswer:
    """The PII state: fixed message, nothing else, no echo of the input."""
    return RenderedAnswer(text=config.PII_MESSAGE, citation=None)


__all__ = [
    "Citation",
    "RenderedAnswer",
    "format_fetched_at",
    "render",
    "render_no_answer",
    "render_pii_blocked",
    "render_refusal",
    "resolve_citation",
]
