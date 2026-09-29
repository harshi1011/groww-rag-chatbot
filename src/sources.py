"""C1 — the source registry: the single source of truth for corpus scope.

The five URLs are fixed by doc/ProblemStatement.txt and listed in doc/PRD.md §5.
Nothing outside this registry may ever be ingested (SC-1), and no URL outside
this registry may ever be cited (SC-3, SC-9).

This module is shared: the ingestion pipeline reads it, the query pipeline
validates against it, and the delivered SOURCES.md is generated from it so the
submission cannot drift from what was actually ingested.

Two name fields, deliberately:
    page_name   the H1 the page itself uses. This is what answers cite, so it
                is the canonical scheme name. Verified against the live page
                during Phase 2 inspection (doc/chunking-strategy.md).
    brief_label the label used in doc/ProblemStatement.txt / doc/PRD.md §5, kept
                for traceability back to the brief. The two differ for one
                scheme, so both are recorded rather than silently reconciled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    """One in-scope public page.

    Attributes:
        source_url: The page URL. This is the value rendered as the citation.
        page_name: Scheme name as written on the page (its H1). Citation name.
        brief_label: Scheme label used in the brief / PRD, for traceability.
        doc_type: Kind of page. `section` on a chunk refines this.
    """

    source_url: str
    page_name: str
    brief_label: str
    doc_type: str

    @property
    def slug(self) -> str:
        """Last path segment of the URL, used as a stable short handle."""
        return self.source_url.rstrip("/").rsplit("/", 1)[-1]


SOURCES: tuple[Source, ...] = (
    Source(
        source_url=(
            "https://groww.in/mutual-funds/"
            "hdfc-large-cap-fund-direct-growth"
        ),
        page_name="HDFC Large Cap Fund Direct Growth",
        brief_label="HDFC Large Cap Fund — Direct Growth (Large Cap)",
        doc_type="scheme-page",
    ),
    Source(
        source_url=(
            "https://groww.in/mutual-funds/"
            "hdfc-equity-fund-direct-growth"
        ),
        # The URL slug says "equity-fund", but the page served at this URL is
        # titled "HDFC Flexi Cap Direct Plan Growth" (the scheme was renamed).
        # Verified during Phase 2 inspection; see doc/chunking-strategy.md.
        page_name="HDFC Flexi Cap Direct Plan Growth",
        brief_label="HDFC Equity Fund — Direct Growth (Flexi Cap)",
        doc_type="scheme-page",
    ),
    Source(
        source_url=(
            "https://groww.in/mutual-funds/"
            "hdfc-elss-tax-saver-fund-direct-plan-growth"
        ),
        page_name="HDFC ELSS Tax Saver Fund Direct Plan Growth",
        brief_label="HDFC ELSS Tax Saver Fund — Direct Plan Growth (ELSS)",
        doc_type="scheme-page",
    ),
    Source(
        source_url=(
            "https://groww.in/mutual-funds/"
            "hdfc-small-cap-fund-direct-growth"
        ),
        page_name="HDFC Small Cap Fund Direct Growth",
        brief_label="HDFC Small Cap Fund — Direct Growth (Small Cap)",
        doc_type="scheme-page",
    ),
    Source(
        source_url=(
            "https://groww.in/mutual-funds/"
            "hdfc-balanced-advantage-fund-direct-growth"
        ),
        page_name="HDFC Balanced Advantage Fund Direct Growth",
        brief_label="HDFC Balanced Advantage Fund — Direct Growth (Balanced Advantage)",
        doc_type="scheme-page",
    ),
)

#: Every in-scope URL. Used for containment checks in both pipelines.
SOURCE_URLS: frozenset[str] = frozenset(source.source_url for source in SOURCES)


def is_allowed_url(url: str) -> bool:
    """Return True only for a URL present in the registry."""
    return url in SOURCE_URLS


def get_source(url: str) -> Source | None:
    """Return the registry entry for `url`, or None if it is out of scope."""
    for source in SOURCES:
        if source.source_url == url:
            return source
    return None


# --------------------------------------------------------------------------
# Query-time scheme aliases
# --------------------------------------------------------------------------
#: Alias phrase -> registry slug, applied to the *question* at query time only.
#:
#: The brief and doc/PRD.md §6 ask about "HDFC Equity Fund", but the page served
#: at that URL is titled "HDFC Flexi Cap Direct Plan Growth" (the scheme was
#: renamed). The corpus therefore contains the string "Flexi Cap" 29 times and
#: "Equity Fund" 0 times, so a question using the brief's name carries no scheme
#: signal and retrieves another scheme's chunks. Measured: asking for the Equity
#: Fund's exit load ranked a Large Cap chunk first and missed the correct chunk
#: entirely (it sits at rank 6, outside top-5).
#:
#: This is a query-time rewrite and nothing else. The chunks, their embeddings,
#: the Chroma collection, and the stored metadata are all untouched, so the
#: corpus hash and every Phase 2/3 artefact stay valid.
#:
#: The slug form is included because chunk ids and the URL both use it, and a
#: question quoting either should resolve to the same scheme.
SCHEME_ALIASES: dict[str, str] = {
    "hdfc equity fund": "hdfc-equity-fund-direct-growth",
    "hdfc-equity-fund": "hdfc-equity-fund-direct-growth",
}

#: Compiled once, whole-word and case-insensitive, so "Equity Fund" never
#: matches inside another word and the bare word "fund" is never rewritten.
_ALIAS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"\b{re.escape(alias)}\b", re.I), slug)
    for alias, slug in SCHEME_ALIASES.items()
)


def _slug_of(source_url: str) -> str:
    return source_url.rstrip("/").rsplit("/", 1)[-1]


#: slug -> canonical page name, i.e. the name that appears in the corpus.
_CANONICAL_BY_SLUG: dict[str, str] = {
    _slug_of(s.source_url): s.page_name for s in SOURCES
}


def canonical_name(slug: str) -> str | None:
    """The corpus name for a registry slug, or None if the slug is unknown."""
    return _CANONICAL_BY_SLUG.get(slug)


def alias_problems() -> list[str]:
    """Return a list of alias-mapping problems; empty means the map is sound.

    Guards against the map drifting away from the registry: a slug that no
    longer exists would silently stop rewriting anything, quietly restoring the
    bug this mapping exists to remove.
    """
    problems: list[str] = []
    for alias, slug in SCHEME_ALIASES.items():
        canonical = _CANONICAL_BY_SLUG.get(slug)
        if not canonical:
            problems.append(
                f"alias {alias!r} points at unknown registry slug {slug!r}"
            )
        elif alias.lower() in canonical.lower():
            problems.append(
                f"alias {alias!r} is already the canonical name; it is a no-op"
            )
    return problems


def expand_aliases(question: str) -> str:
    """Rewrite alias scheme names in a question to their corpus name.

    The alias is *replaced* rather than the canonical name appended, because
    appending dilutes the rest of the question: measured over the two affected
    questions, replacement put the correct chunk at rank 2 where appending left
    it at rank 3, and it also corrected the SID question's top-1 outright.

    Returns the question unchanged when no alias is present, so every other
    query embeds byte-identical text to before and the calibrated threshold
    bands are unaffected.
    """
    text = question or ""
    for pattern, slug in _ALIAS_PATTERNS:
        canonical = _CANONICAL_BY_SLUG.get(slug)
        if canonical:
            text = pattern.sub(canonical, text)
    return text
