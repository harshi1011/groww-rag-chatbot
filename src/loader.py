"""C2 + C3 — fetch and normalise the in-scope source pages.

Phase 2 step 2.0 finding: every registry URL returns HTTP 200 and the scheme
facts are present in the **initial server-rendered HTML**, so this loader uses
the standard library only. No headless browser and no third-party HTML library
were needed. See doc/chunking-strategy.md §1 for the evidence.

Only URLs present in `src.sources.py` may be fetched (SC-1, SC-9).

Normalisation is a documented, ordered pipeline so each step can be inspected
and tested on its own:

    1. strip non-content elements (script/style/noscript/comments)
    2. block tags -> newlines, drop remaining tags, unescape entities
    3. collapse whitespace, drop blank lines
    4. trim to the core region (page H1 .. footer breadcrumb)
    5. drop out-of-scope regions and out-of-scope fields
    6. join `label` + `value` line pairs into one `label: value` unit
    7. attach the nearest preceding section heading
    8. drop degenerate lines
"""

from __future__ import annotations

import gzip
import hashlib
import html as html_lib
import re
import ssl
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src import config
from src.sources import Source, SOURCES, is_allowed_url

# --- network ---------------------------------------------------------------

_HTTP_TIMEOUT = 45
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
}

# --- html -> text ----------------------------------------------------------

_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_SCRIPT = re.compile(r"<script\b.*?</script>", re.S | re.I)
_RE_STYLE = re.compile(r"<style\b.*?</style>", re.S | re.I)
_RE_NOSCRIPT = re.compile(r"<noscript\b.*?</noscript>", re.S | re.I)
_RE_TAG = re.compile(r"<[^>]+>")
# Block-level tags become line breaks so a label and its value stay separable.
_RE_BLOCK = re.compile(
    r"</?(?:p|div|section|article|main|header|footer|nav|aside|ul|ol|li|"
    r"table|thead|tbody|tfoot|tr|td|th|h[1-6]|br|hr|dl|dt|dd|figure|"
    r"figcaption|blockquote|pre|label|button|form|option)\b[^>]*>",
    re.I,
)
_RE_WS = re.compile(r"[ \t\u00a0\u200b\u202f]+")

# --- line classification ---------------------------------------------------

_RE_VALUE = re.compile(r"^[\s\u20b9$€£]*[\d,]+(?:\.\d+)?\s*(?:%|Cr|BP)?\s*$", re.I)
_RE_HAS_DIGIT = re.compile(r"[\d\u20b9]")
_RE_JUNK = re.compile(r"^[\s;)\-–—_=.|]*$")
_LABELS_LOWER = frozenset(label.lower() for label in config.LABEL_VOCABULARY)


def _is_label(line: str) -> bool:
    """True if the line is one of the field labels observed on the pages."""
    return line.lower() in _LABELS_LOWER


def _is_badge(line: str) -> bool:
    """True if the line is a header badge (risk rating or lock-in period).

    The lock-in badge is matched with `search` because it is rendered as
    "ELSS • 3Y Lock-in" and does not start with the keyword. Getting this wrong
    deletes the only statement of the ELSS lock-in period on the page.
    """
    return bool(config.RISK_BADGE.match(line) or config.LOCKIN_BADGE.search(line))


def _is_heading(lines: list[str], index: int) -> bool:
    """True if the line at `index` is a section heading rather than content.

    "Exit Load" is ambiguous: it is the exit-load section heading, and also a
    glossary term in the "Understand terms" panel. It is a heading only when a
    dated entry follows it, which is what the real section looks like.
    """
    line = lines[index]
    if any(re.match(p, line, re.I) for p in config.SECTION_PATTERNS):
        return True
    if re.match(r"^Exit load$", line, re.I):
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        return bool(config.FULL_DATE.match(nxt))
    return False


def _section_name(line: str) -> str:
    """Canonical section label for a heading line."""
    if re.match(r"^About\b", line, re.I):
        return config.ABOUT_SECTION
    return line


def _joined(label: str, value: str) -> str:
    """Combine a fact with its label, rendering an absent value explicitly."""
    rendered = (
        config.NIL_RENDERING if value.strip() == config.NIL_VALUE else value
    )
    return rendered if label == value else f"{label}: {rendered}"


def _drop_sentences(text: str) -> str:
    """Remove whole sentences that carry a field excluded elsewhere.

    The About summary states the live NAV inside a sentence shared with the AUM.
    NAV is excluded everywhere else, so the sentence is dropped and the rest of
    the paragraph is kept rather than discarding the launch date and fund
    manager along with it.
    """
    sentences = _sentence_split(text)
    if len(sentences) <= 1:
        return text
    kept = [
        s for s in sentences
        if not any(re.search(p, s) for p in config.DROP_SENTENCE_PATTERNS)
    ]
    return " ".join(kept) if kept else text


def _sentence_split(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
    return [p for p in parts if p.strip()]


# --- results ---------------------------------------------------------------


@dataclass
class Unit:
    """One normalised fact: a `label: value` pair or a standalone fact line."""

    text: str
    section: str
    label: str | None = None


@dataclass
class LoadedDocument:
    """A fetched, normalised source page plus the provenance of every line."""

    source: Source
    status: int
    resolved_url: str
    fetched_at: str
    html_bytes: int
    raw_line_count: int
    core_line_count: int
    content_hash: str
    units: list[Unit] = field(default_factory=list)
    drop_log: dict[str, int] = field(default_factory=dict)

    @property
    def normalised_chars(self) -> int:
        return sum(len(u.text) for u in self.units)


# --- fetch -----------------------------------------------------------------


def fetch(url: str) -> tuple[int, str, str, int]:
    """Fetch `url` and return (status, resolved_url, html, byte_length)."""
    if not is_allowed_url(url):
        raise ValueError(f"refused: {url!r} is not in the source registry")

    request = urllib.request.Request(url, headers=_HEADERS)
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT,
                                context=context) as response:
        payload = response.read()
        encoding = (response.headers.get("Content-Encoding") or "").lower()
        if "gzip" in encoding:
            payload = gzip.decompress(payload)
        elif "deflate" in encoding:
            try:
                payload = zlib.decompress(payload, -zlib.MAX_WBITS)
            except zlib.error:
                payload = zlib.decompress(payload)
        return (
            response.status,
            response.url,
            payload.decode("utf-8", errors="replace"),
            len(payload),
        )


# --- normalisation steps ----------------------------------------------------


def _strip_non_content(html: str) -> str:
    text = _RE_COMMENT.sub(" ", html)
    text = _RE_SCRIPT.sub(" ", text)
    text = _RE_STYLE.sub(" ", text)
    text = _RE_NOSCRIPT.sub(" ", text)
    return text


def html_to_lines(html: str) -> list[str]:
    """Convert HTML to a flat list of non-empty visible text lines."""
    text = _RE_BLOCK.sub("\n", _strip_non_content(html))
    text = _RE_TAG.sub("\n", text)
    text = html_lib.unescape(text)
    lines = []
    for raw_line in text.split("\n"):
        cleaned = _RE_WS.sub(" ", raw_line).strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def trim_to_core_region(lines: list[str], source: Source) -> list[str]:
    """Cut the global navigation and the footer.

    The core region runs from the page H1 to the footer breadcrumb. Both
    anchors must be found; if either is missing the page has changed shape and
    the caller must fail rather than silently ingest nav boilerplate.
    """
    if source.page_name not in lines:
        raise ValueError(
            f"{source.slug}: page H1 {source.page_name!r} not found "
            f"({len(lines)} visible lines); page structure may have changed"
        )
    start = lines.index(source.page_name)
    end = next(
        (i for i in range(start, len(lines)) if lines[i] == config.FOOTER_ANCHOR),
        None,
    )
    if end is None:
        raise ValueError(
            f"{source.slug}: footer anchor {config.FOOTER_ANCHOR!r} not found; "
            "page structure may have changed"
        )
    return lines[start:end]


def _drop_between(lines: list[str], start_pattern: str,
                  end_pattern: str) -> tuple[list[str], int]:
    """Drop the span from the first start match to the first following end match.

    The end marker is kept, so the following section stays intact.
    """
    start_re = re.compile(start_pattern, re.I)
    end_re = re.compile(end_pattern, re.I)
    kept: list[str] = []
    dropped = 0
    dropping = False
    for line in lines:
        if not dropping and start_re.search(line):
            dropping = True
            dropped += 1
            continue
        if dropping:
            dropped += 1
            if end_re.search(line):
                dropping = False
                kept.append(line)
            continue
        kept.append(line)
    if dropping:  # unterminated span: drop the tail rather than keep noise
        return kept, dropped
    return kept, dropped


def _drop_header_identity(lines: list[str]) -> tuple[list[str], int]:
    """Drop the category tokens between the H1 and the first header badge.

    The header is: H1, then the category tokens ("Equity", "Large Cap", ...),
    then the badges. On the ELSS page the lock-in badge sits first, on the
    others the risk badge does, so the span ends at whichever badge comes first.
    The category is still stated in the About summary sentence, so nothing is
    lost by removing the tokens.
    """
    if not lines:
        return lines, 0
    first_badge = next(
        (i for i in range(1, len(lines)) if _is_badge(lines[i])),
        None,
    )
    if first_badge is None or first_badge < 2:
        # No badge found: keep everything rather than delete a fact.
        return lines, 0
    return [lines[0]] + lines[first_badge:], first_badge - 1


def _drop_returns_ticker(lines: list[str]) -> tuple[list[str], int]:
    """Drop the returns ticker and the NAV quote.

    These sit between the risk badge and the first key-stat label. They are
    performance and live-price data: PRD §4 puts performance out of scope, and a
    stored NAV would contradict the freshness stamp within a day. The span is
    anchored on the risk badge, not on the first badge, so the ELSS lock-in badge
    survives.
    """
    anchor = next(
        (i for i, l in enumerate(lines) if config.FIRST_KEY_STAT.match(l)),
        None,
    )
    if anchor is None or anchor == 0:
        return lines, 0
    badge = next(
        (i for i in range(anchor) if config.RISK_BADGE.match(lines[i])),
        None,
    )
    if badge is None or badge + 1 >= anchor:
        return lines, 0
    dropped = anchor - (badge + 1)
    return lines[: badge + 1] + lines[anchor:], dropped


def _drop_regions(lines: list[str]) -> tuple[list[str], dict[str, int]]:
    """Remove blocks that are out of scope or pure noise (see strategy doc)."""
    log: dict[str, int] = {}
    for name, start, end in config.DROP_REGIONS:
        lines, dropped = _drop_between(lines, start, end)
        if dropped:
            log[name] = dropped
    return lines, log


def _drop_whole_lines(lines: list[str]) -> tuple[list[str], int]:
    """Remove standalone navigation and link lines."""
    kept: list[str] = []
    dropped = 0
    for line in lines:
        if any(re.match(p, line, re.I) for p in config.DROP_LINE_PATTERNS):
            dropped += 1
            continue
        kept.append(line)
    return kept, dropped


def _build_units(lines: list[str]) -> list[Unit]:
    """Turn core lines into units, joining a fact to its label.

    Join rules, in order:
        1. known field label + following content -> "Label: value"
        2. full date + following content      -> "08 May 2015: <entry>"
    Anything else is a standalone unit. A heading sets the current section and
    is not emitted as body text.
    """
    units: list[Unit] = []
    section = config.DEFAULT_SECTION
    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        nxt_joinable = bool(nxt) and not _is_label(nxt) and not _is_heading(
            lines, i + 1
        )

        if _is_heading(lines, i):
            section = _section_name(line)
            i += 1
            continue
        if _is_label(line) and nxt_joinable:
            units.append(
                Unit(text=_joined(line, nxt), section=section, label=line)
            )
            i += 2
            continue
        if config.FULL_DATE.match(line) and nxt_joinable:
            units.append(
                Unit(text=_joined(line, nxt), section=section, label=line)
            )
            i += 2
            continue
        units.append(Unit(text=_joined(line, line), section=section))
        i += 1
    return units


def _drop_fields(units: list[Unit]) -> tuple[list[Unit], dict[str, int]]:
    """Drop out-of-scope fields such as the distributor rating and rank."""
    log: dict[str, int] = {}
    kept: list[Unit] = []
    for unit in units:
        probe = unit.label if unit.label is not None else unit.text
        matched = next(
            (p for p in config.DROP_FIELD_PATTERNS if re.match(p, probe, re.I)),
            None,
        )
        if matched:
            log[matched] = log.get(matched, 0) + 1
            continue
        kept.append(unit)
    return kept, log


def normalise(source: Source, html: str) -> tuple[list[Unit], dict[str, int], int]:
    """Run the full normalisation pipeline for one page."""
    raw_lines = html_to_lines(html)
    core = trim_to_core_region(raw_lines, source)
    core_line_count = len(core)

    core, identity_dropped = _drop_header_identity(core)
    core, ticker_dropped = _drop_returns_ticker(core)
    core, region_log = _drop_regions(core)
    core, line_dropped = _drop_whole_lines(core)

    units, field_log = _drop_fields(_build_units(core))

    units = [u for u in units if len(u.text) >= config.MIN_UNIT_CHARS]
    units = [u for u in units if not _RE_JUNK.match(u.text)]
    units = [u for u in units if u.text.strip() != source.page_name]

    log = dict(region_log)
    if identity_dropped:
        log["header_category_tokens"] = identity_dropped
    if ticker_dropped:
        log["header_returns_ticker_and_nav"] = ticker_dropped
    if line_dropped:
        log["navigation_link_lines"] = line_dropped
    log.update(field_log)
    return units, log, core_line_count


# --- orchestration ----------------------------------------------------------


def load_source(source: Source) -> LoadedDocument:
    """Fetch and normalise a single registry source.

    Raises on any failure; `load_all` records the failure and continues so a
    partial corpus is reported rather than presented as complete.
    """
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status, resolved_url, html, html_bytes = fetch(source.source_url)
    if status != 200:
        raise ValueError(f"{source.slug}: unexpected HTTP status {status}")

    units, drop_log, core_line_count = normalise(source, html)
    content_hash = hashlib.sha256(
        "\n".join(u.text for u in units).encode("utf-8")
    ).hexdigest()[:16]

    return LoadedDocument(
        source=source,
        status=status,
        resolved_url=resolved_url,
        fetched_at=fetched_at,
        html_bytes=html_bytes,
        raw_line_count=len(html_to_lines(html)),
        core_line_count=core_line_count,
        content_hash=content_hash,
        units=units,
        drop_log=drop_log,
    )


def load_all(
    sources: tuple[Source, ...] = SOURCES,
) -> tuple[list[LoadedDocument], list[tuple[str, str]]]:
    """Load every source. Returns (documents, [(url, error), ...])."""
    documents: list[LoadedDocument] = []
    failures: list[tuple[str, str]] = []
    for source in sources:
        try:
            documents.append(load_source(source))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            failures.append((source.source_url, f"{type(exc).__name__}: {exc}"))
    return documents, failures


__all__ = [
    "LoadedDocument",
    "Unit",
    "fetch",
    "html_to_lines",
    "load_all",
    "load_source",
    "normalise",
    "trim_to_core_region",
]
