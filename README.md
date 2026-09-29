# HDFC Mutual Fund Facts-only FAQ Assistant

A retrieval-augmented Q&A app that answers published facts about five HDFC Mutual
Fund schemes, from five public scheme pages and nothing else. Every factual
answer carries one link to the page the fact was read from, is at most three
sentences, and is dated from when that page was actually fetched. It does not
give advice and does not discuss returns.

- `SOURCES.md` — the five pages, generated from the ingestion registry
- `DISCLAIMER.md` — the exact string the UI shows
- `samples/sample_qa.md` — ten real questions with the answers the app gave
- `doc/` — PRD, architecture, implementation plan, chunking proposal

---

## Setup

Python 3.12, CPU only. No GPU, no Docker, no orchestration framework.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # then paste your Groq key into GROQ_API_KEY
```

Ingestion runs once, offline. It fetches the five pages, normalises them, chunks
them, embeds them, and writes a persistent Chroma store:

```powershell
.\.venv\Scripts\python.exe ingest.py
```

Then start the app:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

`GROQ_API_KEY` is read from `.env`, which is git-ignored. It is never written
into a source file, never logged, and never included in an error message. If it
is missing the app says which variable is missing and stops, rather than
failing on your first keystroke.

### Re-running ingestion

`ingest.py` is idempotent. If the store was built from exactly the corpus it
would build now, it reports "already ingested" and writes nothing. If the
registry or the pages have changed, it refuses to replace the existing store
silently and tells you to use `--force`, which deletes the store and rebuilds it
from scratch. The app never triggers ingestion, on load or ever; a missing store
produces a message naming the command and nothing else.

### Verification

Each phase has a suite, and each exits non-zero on failure:

```powershell
.\.venv\Scripts\python.exe ingest.py --verify-only --phase 2   # loader + chunks
.\.venv\Scripts\python.exe ingest.py --verify-only --phase 3   # store + embeddings
.\.venv\Scripts\python.exe ingest.py --verify-only --phase 4   # guardrails
.\.venv\Scripts\python.exe ingest.py --verify-only --phase 5   # query pipeline
.\.venv\Scripts\python.exe ingest.py --verify-only --phase 6   # UI + deliverables
.\.venv\Scripts\python.exe ingest.py --verify-only --phase all
```

Phase 2 fetches the live pages, so it also proves they are still reachable and
still have the shape the normaliser expects. Phases 4–6 need a store, a Groq key,
and Streamlit; phase 6 drives the real `app.py` through Streamlit's own test
harness rather than reimplementing the UI, so what it checks is what a user sees.

---

## Scope

One AMC, five schemes, five pages. HDFC Mutual Fund, via its public pages on
Groww (a fund distributor):

| Scheme | Page |
|---|---|
| HDFC Large Cap Fund Direct Growth | <https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth> |
| HDFC Flexi Cap Direct Plan Growth | <https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth> |
| HDFC ELSS Tax Saver Fund Direct Plan Growth | <https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth> |
| HDFC Small Cap Fund Direct Growth | <https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth> |
| HDFC Balanced Advantage Fund Direct Growth | <https://groww.in/mutual-funds/hdfc-balanced-advantage-fund-direct-growth> |

`SOURCES.md` is generated from the same registry the ingestion pipeline fetches
from, so the delivered list cannot drift from the ingested corpus. Run
`.\.venv\Scripts\python.exe -m src.deliverables` to regenerate it and
`DISCLAIMER.md`; Phase 6 verification fails if either is stale.

### What it answers

Expense ratio, exit load, minimum SIP and lump-sum amounts, ELSS lock-in,
riskometer, benchmark, minimum investments, stamp duty, tax implication, fund
objective, AUM, custodian, registrar, scheme documents, and the glossary terms
the pages define.

### What it refuses

Advice, suitability, and portfolio questions, with a fixed message and an
educational link. Performance questions, with a redirect to the official
factsheet and no figure. Questions about other funds, other AMCs, or other asset
classes. Anything containing a PAN, Aadhaar number, account or folio number, OTP,
email address, or phone number, which is declined without echoing the value and
without storing it.

---

## How it works

```
OFFLINE, ONCE                    ingest.py
  sources.py ──► loader ──► chunker ──► embedder ──► vectorstore
  5 URLs          fetch +     one fact   all-MiniLM   data/chroma
                  normalise   per chunk  384-dim     data/manifest.json
                                                     data/chunks.txt

QUERY, PER QUESTION              app.py
  guardrails (PII, intent)
        │
        ▼
  memory rewrite ──► embedder ──► retrieval (top-5, cosine, threshold 0.45)
   follow-ups only      same           │
   resolved first       model          ▼
                                  prompt ──► Groq ──► output guard
                                                        │
                            one resolved citation ◄────┘
                            + source-derived freshness stamp
```

Design detail lives in `doc/architecture.md`. Four decisions carry most of the
weight:

**Citations cannot be fabricated.** The model never writes a URL. It returns a
`chunk_id`, which is resolved to a stored `source_url` in application code, and
the output guard rejects any answer containing a bare URL. A bad or missing
`chunk_id` falls back to the top-ranked chunk's URL, so a link is always exactly
one and always resolvable. A link that cannot be resolved does not get drawn.

**The freshness stamp is source-derived.** It is formatted from the chunk's
`fetched_at`, the moment the page was read. The UI never calls `date.today()`:
a timestamp generated at question time would claim the page was read now.

**Retrieval fails closed.** A question whose best match is further than the
calibrated cosine threshold is answered with "not in my sources" plus the nearest
topics, before any model call. The threshold was calibrated from observed scores
across the PRD's own questions, not guessed; the three score bands it separates
are documented in `src/config.py`.

**The app cannot reach ingestion.** `app.py` imports no loader, no chunker, and
no store-writing code. Phase 5 and 6 verification both assert this by walking
the imports, because the property that matters is not that it looks true but that
nothing can make it false.

---

## Known limits

**Two of the brief's own example questions have no answer in this corpus.** "How
do I download my capital-gains statement?" and "What documents are needed for
tax reporting?" are questions the brief names, but the five scheme pages do not
carry the instructions or the list. The assistant says the information is not in
its sources. It does not improvise an answer, and it does not reach for a page
outside the approved five. Closing this gap means adding the statement and
tax-document guide pages to the registry, which is a scope change, not a code
change.

**One scheme is named by two names.** The URL `hdfc-equity-fund-direct-growth`
serves a page titled *HDFC Flexi Cap Direct Plan Growth*: the scheme was renamed
and the URL kept the old slug. Both names are recorded in the registry, a
question using the brief's name is rewritten to the corpus name before retrieval,
and the citation label shows the name the page itself uses.

**The corpus is small and frozen.** 144 chunks from five pages, ingested on
28 September 2026. Every answer is as current as that snapshot, and the
freshness stamp says so. There is no re-fetch on load, no scheduled refresh, and
no incremental update; refreshing means re-running `ingest.py`, and if the pages
have not changed it will correctly do nothing.

**MiniLM is a small embedder and the threshold is a blunt instrument.** It works
on this corpus because the questions and the facts are short, phrased alike, and
about a handful of schemes. It would not survive a corpus of long prose or a
much larger scheme list without recalibration. The threshold is a single cosine
cutoff: it does not understand whether a question is a near-miss or a different
subject, only how far away it is.

**Only direct-growth plans.** The five pages are all Direct Growth variants. The
Regular and Direct Growth expense ratios of the same scheme differ, so nothing
here generalises to another plan of the same scheme.

**The fact-checker is lexical, not semantic.** The output guard confirms the
numbers in an answer appear in the retrieved chunks. It cannot confirm that a
sentence describing those numbers is true, only that its figures are borrowed
rather than invented.

**The app is local-first.** `data/chroma/` is git-ignored, so a fresh clone has
no store and must run `ingest.py` once. A hosted deployment must ship the store
and the model weights as build artefacts, and free-tier hosts will be slow to
start because `all-MiniLM-L6-v2` loads on first use.

**Hosting is not decided.** The brief accepts a hosted link or a demo video. The
app has been verified launching headless on a given `PORT`, but no public
deployment is configured yet.

---

## Layout

```
app.py                  the UI: a formatting layer over the verified pipeline
ingest.py               the one-time offline pipeline
src/
  config.py             every path, model setting, threshold, and fixed string
  sources.py            the 5-URL registry — the single source of scope
  loader.py             fetch + normalise (standard library only)
  chunker.py            one fact per chunk
  embedder.py           the one shared embedding code path
  vectorstore.py        persistent Chroma + manifest
  retrieval.py          embed, top-k, relevance gate
  prompt.py             context assembly and the output contract
  llm.py                Groq client, key from .env only
  guardrails.py         PII, intent, output, grounding checks
  render.py             one resolved citation + freshness stamp
  memory.py             bounded conversation memory, follow-up rewriting
  answer.py             the composed query pipeline
  preflight.py          startup checks: store, manifest, model, registry drift
  deliverables.py       generates SOURCES.md and DISCLAIMER.md
  verify*.py            the phase verification suites
data/                   generated; chunks.txt is the reviewable artefact
doc/                    PRD, architecture, implementation, chunking proposal
```

## Requirements notes

`requirements.txt` is fully pinned and matches what the code imports. Two pins
are not obvious and both are explained there:

- **torch is pinned to a CPU build** (`torch==2.5.1+cpu` from the PyTorch CPU
  index), because the default Windows wheel is a large CUDA build this app does
  not need, and the newer CPU wheel does not load on this machine.
- **chromadb is pinned to its pure-Python `SegmentAPI` backend** via
  `chroma-hnswlib`, because the prebuilt Rust core segfaults on write here at
  every version tested.

No HTTP or HTML client was added: all five pages render their facts in the first
server-rendered response, so the loader uses `urllib` and regular expressions
only. That was measured in Phase 2 before the loader was written, not discovered
afterwards; the measurements are in `doc/chunking-strategy.md` §1.
