# Chunking strategy — Phase 2

Written **before** `src/chunker.py`, as `doc/implementation.md` requires: the
strategy is derived from the measured pages, not chosen first and justified
afterwards.

Scope: how the five in-scope Groww pages become chunks. Embedding, the vector
store, retrieval, and guardrails are Phase 3+ and are out of scope here.

---

## 1. Source accessibility and rendering

All five registry URLs were fetched on 2026-09-28. The verdict is **Outcome A —
server-rendered, no browser required**.

| # | Source | HTTP | Initial HTML usable | Core region | Normalised units | Chunks |
|---|--------|------|---------------------|-------------|------------------|--------|
| 1 | HDFC Large Cap Fund Direct Growth | 200 | yes | 445 lines | 29 | 29 |
| 2 | HDFC Flexi Cap Direct Plan Growth | 200 | yes | 584 lines | 27 | 27 |
| 3 | HDFC ELSS Tax Saver Fund Direct Plan Growth | 200 | yes | 475 lines | 28 | 28 |
| 4 | HDFC Small Cap Fund Direct Growth | 200 | yes | 598 lines | 30 | 30 |
| 5 | HDFC Balanced Advantage Fund Direct Growth | 200 | yes | 1680 lines | 30 | 30 |

- Every page carries its facts in the **first HTML response**. No fact required
  JavaScript execution, so `outcome C` does not apply and no headless browser
  was added to the project.
- Every page also contains a `__NEXT_DATA__` JSON island, but it is not needed:
  the visible text is already complete, and parsing it would add a second source
  of truth for the same numbers.
- Consequently `src/loader.py` uses the standard library only (`urllib`,
  `html.parser` via regular expressions). **No dependency was added**, and
  `requirements.txt` is unchanged by this phase.

### Verified consequence

A fetch failure is reported per source and never silently skipped:
`load_all()` returns `(documents, failures)` and `ingest.py` exits non-zero if
any source failed, so a partial corpus cannot be presented as a complete one.

## 2. Why the page name is not the brief's label

`hdfc-equity-fund-direct-growth` serves a page titled **HDFC Flexi Cap Direct
Plan Growth** — the scheme was renamed and the URL kept the old slug. The
approved URL is ingested unchanged (SC-1), and `src/sources.py` records both
names:

- `page_name` — the H1 the page uses, and the name answers cite.
- `brief_label` — the label from `doc/ProblemStatement.txt` / PRD §5, kept for
  traceability.

Silently reconciling them would let the assistant answer "HDFC Equity Fund"
questions with Flexi Cap facts under a name the user never used. Both names are
stored and the difference is reported.

## 3. Measured shape of the data

Measured across all five pages, core region only:

| Metric | Value |
|--------|-------|
| `label: value` units | 755 |
| unit length p50 / p75 / p90 / p95 / p99 | 13 / 21 / 32 / 49 / 106 chars |
| unit length max | 174 chars |
| longest visible line | 384 chars |
| lines over 700 chars | 0 |

**The data is a list of very small discrete facts, not continuous prose.** The
median fact is 13 characters. This single measurement drives every chunking
decision below.

## 4. Normalisation: what is kept and what is dropped

Order of operations, implemented in `src/loader.py`:

1. Strip `script`, `style`, `noscript` and comments.
2. Block tags become line breaks; remaining tags are removed; entities unescaped.
3. Collapse whitespace, drop blank lines.
4. **Trim to the core region**: from the page H1 to the footer breadcrumb
   (`Home`). Both anchors must be found, or the page fails loudly — nav and
   footer boilerplate is never allowed to reach the corpus.
5. Drop the out-of-scope blocks listed below.
6. Join each fact with its label.
7. Attach the nearest preceding section heading.
8. Drop fragments.

### 4.1 Blocks removed, with measured counts

Counts are per page; the balanced-advantage page is the largest.

| Block | Why removed | Lines dropped |
|-------|-------------|---------------|
| Return calculator + historic returns | Performance figures; PRD §4 puts returns out of scope | 27 |
| Holdings table | ~50–350 rows of constituents; not a fact type the brief asks for, and it is the bulk of the page (up to 1310 lines) | 206–1310 |
| Returns and rankings | Return figures and a category rank; PRD §4 excludes rankings | 24 |
| Compare similar funds | Other funds' returns; the other schemes named are out of corpus scope | 27 |
| Fund management cards | Not a fact type the brief or PRD asks for, and each card's "also manages these schemes" list names schemes **outside the five**, which invites citing a scheme the corpus does not cover (SC-9) | 66–70 |
| AMC contact block | Phone, e-mail, website, launch date, address. PRD §4 sets a no-PII stance; `Launch Date` duplicates `Date of Incorporation` | 11 |
| RTA contact block | Registrar e-mail, website, address; runs to the end of the region | 6 |
| Header category tokens | "Equity", "Large Cap"; still stated in the About summary sentence | 2–3 |
| Returns ticker + NAV quote | `3Y annualised`, `1D/1M/6M/1Y/3Y/5Y/All`, and the live NAV. Performance is out of scope, and a stored NAV would contradict the freshness stamp within a day | 13 |
| Nav link lines | `Check past data`, `View details`, `See All`, `Compare` | 3–9 |

Fields removed by label: `Rating` (a distributor star rating, a ranking),
`Rank (total assets)` (a ranking). The `NAV:` line is removed with the ticker.

The `Fund management` decision is the one judgement call worth stating plainly:
it deletes real, well-formed content. It is justified by SC-9 — leaving 150+
units of other HDFC scheme names in the corpus is the single largest risk of the
assistant naming a scheme it has no source for. The current fund manager is
still named in the retained About summary sentence, so no answerable fact is
lost.

### 4.2 What is kept

Seven sections across five documents, producing **144 chunks** (27–30 per
document):

`Key facts` (lock-in badge, risk rating, min SIP, AUM, expense ratio),
`Minimum investments`, `Understand terms` (glossary definitions),
`Exit Load` (dated entries), `Exit load, stamp duty and tax`, `Tax implication`,
`About` (objective, benchmark, SID field, fund house, total AUM, incorporation
date, custodian, registrar).

Every fact type the brief names is present and verified per document: expense
ratio, exit load, minimum SIP, minimum 1st/2nd investment, ELSS lock-in, risk
rating, benchmark, AUM, objective, custodian, registrar, stamp duty, tax
implication.

## 5. Chunk size and overlap

**One fact per chunk, packed from units, target 320 chars, hard cap 700 chars,
zero overlap.**

| Setting | Value | Why |
|---------|-------|-----|
| `CHUNK_TARGET_CHARS` | 320 | Packs a multi-sentence unit (the About summary is 384 chars) into whole sentences. ≈80 tokens, well inside MiniLM's 256-token window. |
| `CHUNK_MAX_CHARS` | 700 | No measured line exceeds 384, so 700 is headroom, not a fitted value. It also matches architecture §10.1's stated cap. |
| `CHUNK_OVERLAP_SENTENCES` | 1 | Applies **only** when one unit is too long to be a single chunk. |
| `MIN_UNIT_CHARS` | 8 | Drops fragments (`%`, `--`, a stray bracket). Set far below the 13-char median on purpose: short facts are the common case, not junk. |

### Why not prose-sized chunks

The default 1000–1500 char chunk would hold roughly 30–70 facts at this data's
shape. That breaks two explicit requirements:

- architecture §5 / PRD §9: **one fact, or one clearly labelled section, per
  chunk.** A chunk of 70 mixed facts cannot produce a ≤3-sentence answer.
- The retrieval contract: a single retrieved chunk must be able to answer a
  question without a second, conflicting chunk.

### Why zero overlap

Overlap exists so a sentence split across a chunk boundary is not lost. The units
here are short, self-contained facts, so there is nothing to overlap. Adding it
would actively harm the corpus: the same figure would appear in two chunks under
different section names, and the assistant could cite either.

Overlap is therefore used on exactly one path — a unit longer than
`CHUNK_MAX_CHARS` is split on sentence boundaries and the final sentence is
repeated as the first sentence of the next chunk, so no sentence is ever lost.
Measured: this affects the About summary paragraph only (384 chars → 2 chunks).

### Never split a fact

A `label: value` unit is joined **before** chunking, so a figure is never
separated from its label. This is the specific failure the brief calls out
("a number separated from its label"). There is no overlap to protect here
because the pair is atomic.

## 6. Chunk text and metadata

Each chunk's text is prefixed with its own context, so it is self-describing
both to the embedder and to the answering model:

```
HDFC ELSS Tax Saver Fund Direct Plan Growth — Key facts: Expense ratio: 1.21%
```

The prefix costs ~50 chars and is justified by the measurement: a bare
`1.21%` is 5 characters and carries no meaning, so a 384-dimension embedding of
it would be close to meaningless. Prefixing means the embedder sees the scheme,
the section, and the fact.

Metadata per chunk, matching architecture §10.3 exactly:

| Field | Value |
|-------|-------|
| `chunk_id` | `{slug}-{index:03d}`, stable and deterministic |
| `source_url` | The registry URL, always a citation (SC-9) |
| `source_title` / `scheme_name` | The page H1 |
| `doc_type` | `scheme-page` |
| `section` | One of the seven sections above; becomes the citation label |
| `chunk_index` | 0-based position within the document |
| `fetched_at` | UTC ISO-8601 fetch time; becomes the freshness stamp |
| `content_hash` | SHA-256 of the document's normalised text, so drift and re-ingest are detectable |

## 7. Corpus gaps recorded, not papered over

- **Capital-gains statement / tax-document downloads: not answerable.** None of
  the five pages contains download instructions for statements, tax reports, or
  the capital-gains statement. The example question in `src/config.py` will
  correctly hit the no-answer path. No substitute source was invented (SC-1).
- **SID / KIM: not answerable.** Each page shows a `Scheme Information
  Document(SID)` label, but the document is a link with no link text in the
  rendered content, so the field is retained as a label with no value.
- **"Riskometer": not a literal field.** The pages use `Very High Risk`. The
  literal word "riskometer" appears nowhere. The risk rating is captured as a
  fact; a question using the word "riskometer" is expected to retrieve it by
  meaning, which is a Phase 5 retrieval-calibration question.
- **Lock-in period is stated only for ELSS**, and only in the header badge
  (`ELSS • 3Y Lock-in`). The other four pages have no lock-in field, which is
  correct: only ELSS has one. This badge is a single line, so the header
  normalisation is anchored on the badges specifically to preserve it.

## 8. Verification performed

Run with `.venv\Scripts\python.exe ingest.py --verify-only`:

1. Every registry URL fetched, HTTP 200 recorded.
2. Exactly one document per registry URL; no URL outside the registry.
3. Every chunk's `source_url` is in the registry.
4. `data/chunks.txt` exists and is non-empty.
5. Required labels present per document: expense ratio, min SIP, min 1st
   investment, min 2nd investment, exit load, risk rating, benchmark, objective.
6. ELSS document contains a lock-in unit.
7. No chunk is empty and no chunk is shorter than the minimum.
8. No chunk is only whitespace, punctuation, or nav/footer boilerplate.
9. No `NAV`, `Rating`, or `Rank (total assets)` in any chunk.
10. Every chunk is one fact or one labelled section; each is a single labelled
    unit and is attributed to a section.
11. No chunk mentions a scheme outside the five in the registry.

Results are recorded in the Phase 2 report, not here.

### 8.1 Result

`63 passed, 0 failed` on 2026-09-28, live sources, no cached HTML. Notable
observations from the run:

- All five URLs fetched, HTTP 200, 452–816 KB of HTML each.
- 144 chunks, 8 distinct sections, all chunk ids unique, all 9 metadata fields
  populated on every chunk.
- Longest chunk body 263 chars, against a 700-char cap: the cap is headroom, and
  the split path fires only for the 384-char About summary.
- No out-of-scope scheme name survived in any chunk, across 30 name patterns.
- `data/chroma/` and `data/manifest.json` confirmed absent, so nothing from
  Phase 3 was written.

### 8.2 Two defects this verification caught

Recorded because they are the reason the checks exist:

1. **The ELSS lock-in was being silently deleted.** The badge renders as
   `ELSS • 3Y Lock-in`, so the pattern must be matched with `search`, not
   `match`. With `match` the line did not register as a badge, so the header
   normalisation classified it as a category token and removed it. That is the
   *only* statement of the lock-in period anywhere on the page, so the
   assistant would have been unable to answer the brief's own example question.
2. **The manager-card and compare-table spans ended one line early.** The end
   anchor `^[A-Z]{2}` also matches the first two letters of `HDFC Value Fund`,
   so each span stopped at the first scheme name and left the rest of the list
   in the corpus. Fixed by anchoring with `$`.

Both were invisible in the drop counts alone and were only caught by asserting
on the facts that must survive.
