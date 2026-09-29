# Implementation Plan — Mutual Fund FAQ Assistant (Facts-Only RAG Chatbot)

**Source of truth:** `doc/ProblemStatement.txt` → `doc/PRD.md` → `doc/architecture.md`
**Status:** Plan only · **This document contains no application code.** Commands appear only as
operational invocations (`pip install`, `python ingest.py`, `streamlit run app.py`).
**Phase structure:** exactly the six phases below, in order. Every phase has a gate; a gate is not
passed, the next phase does not start.

---

## 0. How to read this plan

Each phase specifies: **Objective → Files → What it implements → Inputs/Outputs → Verification →
Gate.** Verification steps are written to be executed literally, with an expected result for each,
so a reviewer can reproduce them.

### 0.1 Pipeline ownership (ingestion vs. query-time)

The brief requires ingestion to run **once**, not on every restart (SC-10). Ownership is fixed by
architecture §9 and is enforced by the file layout — this table is the contract.

| Pipeline | Entry point | Files owned | May NOT contain |
|---|---|---|---|
| **Offline (ingestion)** | `ingest.py` | `src/sources.py`, `src/loader.py`, `src/chunker.py`, write half of `src/vectorstore.py`, `src/embedder.py` (shared) | any UI code |
| **Online (query)** | `app.py` | `src/guardrails.py`, `src/retrieval.py`, `src/prompt.py`, `src/llm.py`, `src/render.py`, read half of `src/vectorstore.py`, `src/embedder.py` (shared) | fetch, chunk, or store-write code paths |
| **Shared** | — | `src/config.py` (paths, model id, k, thresholds, fixed strings), `src/sources.py` (registry read by both) | any pipeline logic |

`app.py` must never import `loader` or the chunk-writing path. Each phase's verification includes a
separation check so drift is caught immediately rather than at submission.

### 0.2 Two decisions deliberately deferred

1. **Chunking strategy** — not decided in this document. Phase 2 inspects the real source data,
   then writes `doc/chunking-strategy.md` **before** any chunking code exists.
2. **Retrieval `k` and the relevance threshold** — not fixed here. Phase 5 calibrates both from the
   observed score distribution and freezes them in config.

---

## Phase 1 — Project setup

### Objective
Create a runnable, secret-safe project skeleton in which both entry points import cleanly, all
dependencies are pinned, configuration is centralised, and the 5-source registry is declared —
with **no** fetching, embedding, or LLM logic yet.

### Files to create or modify

| File | Action | Contents |
|---|---|---|
| `requirements.txt` | create | Pinned deps only: `sentence-transformers`, `chromadb`, `groq`, `streamlit`, plus the HTTP client chosen in Phase 2 and `python-dotenv` |
| `.env.example` | create | `GROQ_API_KEY=` placeholder only, no real value |
| `.gitignore` | create | `.env`, `__pycache__/`, virtualenv, model cache, `data/chroma/`, `data/manifest.json` |
| `src/__init__.py` | create | Empty package marker |
| `src/config.py` | create | C18: all paths, model id `sentence-transformers/all-MiniLM-L6-v2`, expected dimension 384, `normalize_embeddings` flag, cosine distance setting, placeholders for `k` and threshold, all fixed UI strings |
| `src/sources.py` | create | C1: the 5 URLs from PRD §5 with `scheme_name` and `doc_type`; the single source of truth for scope, used by ingestion *and* by the delivered `SOURCES.md` |
| `ingest.py` | create | Skeleton entry point that validates config and exits — no pipeline yet |
| `app.py` | create | Skeleton Streamlit entry point that renders a placeholder and exits — no pipeline yet |
| Git repository | create | `git init` — the workspace is not yet a repo, and SC-14 depends on ignore rules actually being enforced |

### What the phase implements
- C1 (source registry) and C18 (config + secrets) from architecture §1.
- The **entry-point separation** that architecture §9 depends on: two executables, one shared
  package.
- Secret hygiene required by SC-14: key read from `.env`, `.env` ignored, no key literal in source.

### Inputs and outputs
- **Inputs:** `doc/PRD.md`, `doc/architecture.md`, a Groq API key placed in `.env` by the developer.
- **Outputs:** an importable `src` package, a validated config, a 5-entry registry, two runnable
  entry points, an initial commit with no secrets.

### Verification (run in order; each has an expected result)

1. Copy `.env.example` to `.env` and paste the real key. **Expected:** no error.
2. `git check-ignore .env` **returns the path** — the file is ignored.
3. `git status --porcelain` **does not list `.env`**.
4. Search the whole repo for the key's value **returns zero matches** outside `.env`.
5. Import `src.config` in a fresh interpreter. **Expected:** model id, dimension 384, and all paths
   print; the Groq key is **not** printed, and the source list from `src.sources` contains exactly
   5 URLs, character-for-character matching PRD §5.
6. Delete the key from `.env`, then import again. **Expected:** a clear one-line message naming the
   missing variable — never a traceback containing a key.
7. `python ingest.py` **starts and exits cleanly with no network traffic.**
8. `streamlit run app.py` **starts and shows the placeholder page;** then stop it.
9. Model availability check: load `all-MiniLM-L6-v2` once via `sentence-transformers` and embed a
   short sentence. **Expected:** the model downloads from its public source and returns a
   384-length vector **with no API key present in the environment.** This de-risks Phase 3 early.
   (Vectors are not written anywhere yet.)
10. Separation check: inspect `app.py`'s imports. **Expected:** no import of `loader` or `chunker`.

### Gate — must be true before Phase 2
- [ ] All 10 verification steps pass.
- [ ] `.env` is ignored and the key appears nowhere but `.env`.
- [ ] Registry holds exactly the 5 PRD §5 URLs; scope is declared in one place.
- [ ] Both entry points start and exit with no pipeline work.
- [ ] `all-MiniLM-L6-v2` loads locally with no API key and yields 384 dimensions.
- [ ] `requirements.txt` is pinned and installs cleanly into a fresh virtualenv.

---

## Phase 2 — Loading & chunking

> **This phase begins with a data check and ends with a written decision before any chunking code
> exists.** Order within the phase is mandatory: 2.0 → 2.5.

### Objective
Determine how these particular pages can actually be read, fetch and normalise the 5 sources,
inspect the real data, **write `doc/chunking-strategy.md`**, and only then implement a chunker that
satisfies the architecture §2.3 contract, producing the inspectable `data/chunks.txt`.

### 2.0 — Groww page rendering / HTML check *(before deciding the loader implementation)*

**Objective:** determine whether the scheme facts exist in the initial HTML response before any
loader is written. If they do not, plain HTTP fetching is the wrong loader and must not be built.

**Inputs:** the 5 URLs. **Output:** a recorded finding, carried into the strategy document and the
README, plus a loader decision.

Check, for each URL:
1. Request the page and keep the **raw initial HTML**, before any script runs.
2. Search that HTML for the *labels* of the facts this corpus must contain — expense ratio, exit
   load, minimum SIP / minimum investment, lock-in, benchmark, riskometer, scheme name.
3. Classify the result:

| Outcome | Evidence | Loader decision |
|---|---|---|
| **A** | The fact labels and values are present in the initial HTML | Standard HTTP fetch (standard library, or `requests` if already chosen) is sufficient. No extra dependency. |
| **B** | The initial HTML is an app shell, but the values are loaded by a **public JSON/XHR endpoint** visible in the page's network calls | Fetch that public endpoint per scheme. Still no browser dependency. **Confirm the endpoint is a public official/distributor page per PRD §9 before using it, and document it.** |
| **C** | Values require JavaScript rendering and no plain or JSON path exposes them | A rendering-capable fetch becomes **necessary**. Decide at this point, and justify it in the README: the corpus is otherwise unloadable, so the added dependency is required by the data rather than chosen for convenience. |

4. Also record: HTTP status, final resolved URL, response size, and whether a plain request is
   rate-limited (the 5 requests are made once, so this is low risk but must be observed, not
   assumed).
5. Record the outcome for **all five** URLs. If results differ across schemes, the loader must
   handle the actual mix found — not the mix hoped for.

**Decision is recorded, not assumed.** The rest of Phase 2 proceeds only after this classification
exists.

### 2.1 — Loader
**Files:** `src/loader.py`; `requirements.txt` updated if outcome C was found.
**Implements:** C2 (fetch + extract) and C3 (normalise).
- Fetch only URLs present in `src/sources.py`; anything else is rejected.
- Per source, capture: `source_url`, resolved URL, `source_title`, `scheme_name`, `doc_type`,
  `fetched_at`, content hash, extracted text.
- Normalise: strip nav, footer, cookie banners, and repeated boilerplate; collapse whitespace;
  **retain headings** — they are the citation labels.
- On any per-source failure, report the source and continue, then fail the run at the end with a
  non-zero exit. A partially loaded corpus must never look like a complete one.

### 2.2 — Inspect the actual source data *(hand inspection, before any decision)*
**Output:** observations recorded for the strategy document.
- Read the normalised text of all 5 sources end to end.
- Record: which fact types are actually present per source; whether facts sit in tables, labelled
  rows, or prose; the heading structure; the longest single fact-bearing block; whether values
  appear near their labels; any content that must be kept for the "how to download statements" class
  of questions.
- Note explicitly which PRD §6 fact questions the corpus can and cannot support. Facts absent from
  the pages will later surface as "not in my sources" — better discovered now.

### 2.3 — `doc/chunking-strategy.md` *(written before `src/chunker.py` exists)*
**Required contents**, per architecture §2.3 and the brief:
1. **Inspection findings** from 2.0 and 2.2, including the loader outcome classification.
2. **Proposed strategy** — the chunking unit (heading / block / sentence / table-aware), with the
   **chunk size** and **overlap** stated as numbers.
3. **Justification** — why this suits *this* data specifically, referencing the observations from
   2.2 rather than generic best practice.
4. **Metadata kept per chunk** — must at minimum include the architecture §2.4 contract:
   `chunk_id`, `source_url`, `source_title`, `scheme_name`, `doc_type`, `section`, `chunk_index`,
   `fetched_at`, `content_hash`.
5. **How the strategy satisfies the contract** — one fact or one labelled section per chunk, one
   resolvable citation, a human-readable section label, and never a split number.
6. **Known trade-offs** of the chosen strategy.

### 2.4 — Chunker
**Files:** `src/chunker.py`; chunking parameters added to `src/config.py`; `ingest.py` wired to
load → chunk.
**Implements:** C4, driven entirely by the parameters recorded in 2.3 — the strategy lives in
config, so changing it never requires editing pipeline code.

### 2.5 — Chunk dump
**Output:** `data/chunks.txt` — every chunk with its full metadata, human-readable (SC-11).

### Verification (run in order)

1. **Loader completeness.** Run the loader across all 5 sources. **Expected:** 5 successes, each
   reporting status, resolved URL, size, extracted text length, and fetch timestamp. Any failure is
   reported by URL, and the run exits non-zero.
2. **Scope containment.** Point the registry at a non-listed URL. **Expected:** rejection — no
   fetch is attempted (SC-1, SC-9).
3. **Fact presence.** Search the extracted text for the fact *labels* listed in 2.0.
   **Expected:** labels present for the fact types the pages actually carry. Absence is recorded
   as a corpus limitation, not patched.
4. **Normalisation quality.** Read a sample of extracted text from each source. **Expected:** no nav
   menus, no footer, no cookie text; headings intact; whitespace collapsed.
5. **Ordering proof.** Confirm `doc/chunking-strategy.md` exists and contains all six required
   elements, and that `src/chunker.py` was created **after** it. **Expected:** the strategy
   document is the source of the parameters the chunker actually uses.
6. **Metadata completeness.** Open `data/chunks.txt`. **Expected:** every chunk shows all nine
   metadata fields plus its text; no chunk is missing a field.
7. **Citation resolvability.** Collect the distinct `source_url` values in `chunks.txt`.
   **Expected:** the set is a subset of the 5 registry URLs — nothing else appears.
8. **No split numbers.** Manually inspect every chunk containing an expense ratio, exit load, SIP
   amount, lock-in period, or benchmark value. **Expected:** each such value appears complete inside
   a single chunk, with its label.
9. **No degenerate chunks.** Report total chunk count, plus mean, min, and max chunk length.
   **Expected:** no empty chunks and no chunk that is only boilerplate.
10. **One fact per chunk.** Read at least 10 random chunks. **Expected:** each is a single fact or a
    single labelled section — a chunk that would prompt a multi-fact answer is a strategy defect.
11. **Separation check.** `app.py` still imports no loader or chunker.

### Gate — must be true before Phase 3
- [ ] The rendering/HTML check is complete for all 5 URLs, classified A/B/C, and the loader
      decision is recorded.
- [ ] All 5 sources load successfully; failures would have failed the run loudly.
- [ ] `doc/chunking-strategy.md` exists with findings, proposal, justification, chunk size, overlap,
      metadata, contract mapping, and trade-offs — **written before `src/chunker.py`**.
- [ ] `data/chunks.txt` is hand-inspected: complete metadata, no split numbers, no empty or
      boilerplate chunks, one fact per chunk.
- [ ] Every chunk's `source_url` resolves to a registry URL.
- [ ] Known gaps (fact types the corpus cannot support) are recorded for later explanation.

---

## Phase 3 — Embedding & vector store

### Objective
Embed every chunk with the single shared embedder, write chunks + vectors + metadata into a
persistent ChromaDB store, emit the manifest, and make ingestion idempotent so it runs once.

### Files to create or modify

| File | Action | Contents |
|---|---|---|
| `src/embedder.py` | create | C6: the **only** place a `SentenceTransformer` is constructed; one `embed()` path used by both pipelines; dimension assertion; pinned `normalize_embeddings` |
| `src/vectorstore.py` | create | C7 + C8: write half (collection creation with cosine distance, batch upsert) and read half (open, query top-k), plus manifest read/write |
| `data/chroma/` | generated | Persistent store directory |
| `data/manifest.json` | generated | Model id, dimension, normalisation flag, distance metric, chunking config, source list, corpus hash, chunk count, ingested timestamp |
| `ingest.py` | modify | Wire load → chunk → embed → store; add the no-op check and an explicit `--force` flag |
| `requirements.txt` | modify | Already pinned in Phase 1 |

### What the phase implements
- C6, C7, C8.
- The **persist-and-skip** mechanism behind SC-10: if the store exists and the corpus hash matches,
  ingestion exits as a no-op.
- The **provenance** half of the same-model guarantee (architecture §10.3): the model id is written
  into both the manifest and the collection metadata.

### Inputs and outputs
- **Inputs:** chunks in memory from Phase 2; config from Phase 1.
- **Outputs:** `data/chroma/`, `data/manifest.json`, a re-embed verification result, and an
  idempotent `ingest.py`.

### Verification (run in order)

1. **Dimension.** Embed a short string via `src.embedder`. **Expected:** exactly 384 floats.
2. **Determinism.** Embed the same string twice in one process, then again in a fresh process.
   **Expected:** identical vectors within tight numerical tolerance.
3. **Full ingestion.** Run `python ingest.py`. **Expected:** progress per stage (load → chunk →
   embed → store), a final count, and a non-zero exit on any failure.
4. **Count agreement.** **Expected:** the store's collection count equals the chunk count in
   `data/chunks.txt` from Phase 2.
5. **Round-trip fidelity (architecture §10.4).** Re-embed a sample of stored chunk texts and compare
   against the stored vectors. **Expected:** match within tolerance. An unexplained mismatch **fails
   this phase** — it is the defect that later looks like "the bot ignores my corpus".
6. **Metadata in the store.** Read back a few records. **Expected:** text plus all nine metadata
   fields, and `source_url` correct on each.
7. **Manifest completeness.** Open `data/manifest.json`. **Expected:** model id
   `sentence-transformers/all-MiniLM-L6-v2`, dimension 384, normalisation flag, distance metric
   `cosine`, the full chunking configuration, the 5 source URLs, a corpus hash, the chunk count,
   and the ingestion timestamp.
8. **Idempotency (SC-10).** Run `python ingest.py` again. **Expected:** an explicit
   "already ingested, nothing to do" message, no re-embedding, and an unchanged chunk count and
   corpus hash.
9. **Forced re-ingest.** Run with `--force`. **Expected:** the store is rebuilt cleanly — count
   matches `chunks.txt`, with **no duplicated or stale records** left from the previous run.
10. **Dimension-assertion guard.** Temporarily configure an expected dimension that does not match
    the model. **Expected:** ingestion **aborts** with a clear message and does not write a partial
    store. Restore the correct value afterwards.
11. **Persistence across restart.** Record the store directory's file count and size, exit the
    process, reopen the store in a new process. **Expected:** data still present and queryable.
12. **Separation check.** `app.py` still imports no loader, chunker, or store-write function.

### Gate — must be true before Phase 4
- [ ] All 384-dimensional, deterministic, round-trip-verified.
- [ ] `data/chroma/` persists and reopens across processes (SC-10).
- [ ] `data/manifest.json` contains every field listed in step 7.
- [ ] Re-running ingestion is a proven no-op; `--force` rebuilds without duplication.
- [ ] The dimension assertion is proven to abort on mismatch.
- [ ] `app.py` remains free of ingestion imports.

---

## Phase 4 — Guardrails

### Objective
Implement the input-side and output-side guardrails as deterministic, independently testable
functions, plus the fixed strings — so the expensive and risky parts of the pipeline are protected
before any answer can be generated.

### Files to create or modify

| File | Action | Contents |
|---|---|---|
| `src/guardrails.py` | create | C10 (PII screen), C11 (intent classification), C16 (output checks), and the relevance-gate function C13 with its threshold read from config (value calibrated in Phase 5) |
| `src/config.py` | modify | PII patterns, refusal/educational-link constants, the "not in my sources" string, the advice/performance/out-of-scope strings, the relevance threshold placeholder |
| `samples/` | — | Fixture-based checks are run as ad-hoc verification; the only committed sample artefact is `samples/sample_qa.md` in Phase 6 |

### What the phase implements
Architecture §5, guardrails 1, 2 and 5 fully, and guardrail 3 as a callable awaiting calibration:
- **PII** — screen on raw input, **before embedding**; never echo, never store, never log.
- **Intent** — classify `factual` / `advice` / `performance` / `off-topic` / `out-of-scheme`,
  before any model call, so a refusal is never contaminated by retrieved context.
- **Output checks** — citation resolvable, ≤ 3 sentences, no recommendation or return/comparison
  phrasing.
- **Fixed strings** — disclaimer, refusal messages, and educational links are constants in config,
  never generated. This is why a refusal cannot itself drift into advice.

### Inputs and outputs
- **Inputs:** raw user question strings; config constants.
- **Outputs:** a classification, a pass/block decision, and fixed response strings — all as pure
  functions, testable with no store, no model, and no network.

### Verification (run in order)

1. **PII positives.** Feed each PRD §6 PII example — a PAN-shaped value, an Aadhaar-shaped value, a
   long account-number digit run, an OTP mention, an email, a phone number. **Expected:** all
   blocked, none embedded, none returned anywhere in the result.
2. **PII negatives (over-blocking check).** Feed digit-bearing but harmless questions, e.g. a
   question about a lock-in **period expressed in years** and one about a **percentage**. **Expected:**
   not blocked. A PII screen that blocks ordinary fee/period questions is a defect.
3. **No echo, no log.** Run the PII cases with logging at full verbosity and inspect the rendered
   result. **Expected:** the sensitive value appears **nowhere** — not in the response, not in the
   return object, not in stdout (SC-8).
4. **Intent — refusals.** Feed the 5 PRD §6 refusal questions, plus a returns comparison and a
   cross-scheme question naming a different AMC. **Expected:** each classified as advice,
   performance, or out-of-scope; each returns a fixed refusal string plus an educational link, and
   **no LLM call is made.**
5. **Intent — factual (false-positive check).** Feed all 10 PRD §6 factual questions.
   **Expected:** every one classified `factual` and passed through. If any factual question is
   refused, the intent guard is too aggressive and must be tightened before Phase 5 — this is the
   single most damaging failure mode, because a bot that refuses everything looks "safe" and
   useless.
6. **Output checks — accept fixture.** A well-formed 2-sentence answer with a valid `chunk_id`.
   **Expected:** passes.
7. **Output checks — reject fixtures.** (a) a 5-sentence answer; (b) an answer containing a URL or a
   `chunk_id` that is not in the store; (c) an answer containing recommendation phrasing; (d) an
   answer quoting a return figure or ranking two schemes. **Expected:** each is rejected and routed
   to the fallback.
8. **Fallback defined.** Confirm the fallback path returns a templated extractive answer built from
   the top chunk, and contains no free-generated text. **Expected:** defined, deterministic, and
   still able to carry a citation.
9. **Fixed-string integrity.** **Expected:** the disclaimer equals `Facts-only. No investment advice.`
   exactly; all educational links are configured constants pointing at real, public pages — none is
   LLM-generated.
10. **Separation check.** `app.py` still imports nothing from the ingestion path.

### Gate — must be true before Phase 5
- [ ] All PRD §6 PII cases blocked, with no echo and nothing written to logs.
- [ ] All 10 PRD §6 factual questions pass the intent guard; zero false positives.
- [ ] All refusal cases return fixed strings + educational links with **no LLM call**.
- [ ] The output checker accepts the good fixture and rejects all four bad fixtures.
- [ ] The fallback answer path is defined and citation-capable.

---

## Phase 5 — Retrieval + LLM answer

### Objective
Complete the query-time pipeline end to end without a UI: question → embed → retrieve top-k →
relevance gate → prompt → Groq → output guard → renderable answer with exactly one resolved
citation, and calibrate `k` and the relevance threshold from observed data.

### Files to create or modify

| File | Action | Contents |
|---|---|---|
| `src/retrieval.py` | create | C12 (embed question via the shared embedder, query top-k with metadata) and C13 (relevance gate) |
| `src/prompt.py` | create | C14: system rules, context assembly, the ≤ 3-sentence output contract, and the explicit request for a supporting `chunk_id` |
| `src/llm.py` | create | C15: Groq client; key from `.env` only; timeout and failure handling |
| `src/render.py` | create | C17 logic: answer text, one citation link resolved from `chunk_id` → metadata, and `Last updated from sources:` from `fetched_at` |
| `ingest.py` / `app.py` | — | `app.py` gains the startup preflight only; the UI is Phase 6 |
| Scratch driver | temporary | A throwaway script **outside the repo** (or an interpreter session) that calls the same `src` functions the UI will call, so the pipeline is testable before any UI exists. Not committed. |

### What the phase implements
Architecture §3 and §4 in full:
- Top-k retrieval with metadata travelling with the text.
- The relevance gate, which is what makes "unanswered" detectable instead of answered from the
  nearest irrelevant chunk.
- **Citation resolution in application code** — the LLM returns a `chunk_id`; the URL comes from
  stored metadata, so a fabricated link is structurally impossible (SC-3).
- The startup preflight (architecture §9): store exists → manifest exists → model id matches →
  corpus hash matches; refuse to serve on mismatch, and never auto-ingest.

### Inputs and outputs
- **Inputs:** the store and manifest from Phase 3; guardrails from Phase 4; the 10 factual and 5
  refusal questions from PRD §6.
- **Outputs:** a tuned `k` and threshold frozen in config; answers that are ≤ 3 sentences with one
  real citation and a freshness stamp.

### Verification (run in order)

1. **Preflight — missing store.** Point the app config at an empty store path. **Expected:** a
   clear "run ingestion first" message naming the command; **no** ingestion is triggered (SC-10).
2. **Preflight — model mismatch.** Temporarily change the model id in the manifest. **Expected:**
   the app **refuses to serve** and reports that re-ingestion is required. Restore afterwards.
3. **Preflight — corpus drift.** Change a registry entry without re-ingesting. **Expected:** a
   warning that sources changed and re-ingestion is needed.
4. **Retrieval sanity (no LLM).** For each of the 10 factual questions, print top-k with score,
   `chunk_id`, `section`, and a text preview. **Expected:** the fact-bearing chunk is present in
   top-k for every question. **Inspect these by hand** — this is the real test of whether Phase 2's
   chunking worked.
5. **Calibrate `k` and the threshold.** Record the score distribution for the 10 in-scope questions
   and for deliberately off-scope questions. **Expected:** a threshold exists that separates them;
   record the chosen `k`, the threshold, and the reasoning in config. Do not proceed on a guessed
   value.
6. **End-to-end — all 10 factual questions.** **Expected, for every one:** ≤ 3 sentences; exactly
   one citation link; the link resolves to one of the 5 registry URLs; `Last updated from sources:`
   present; every claim traceable to a retrieved chunk.
7. **Freshness stamp correctness.** Compare the stamp against the manifest's ingestion/fetch
   timestamps. **Expected:** it reflects **source** freshness, never the current chat time.
8. **No-fabrication test.** Ask a plausible-sounding question whose fact is **not** in the corpus
   (a fee or limit the pages do not carry). **Expected:** the assistant says the information is not
   in its sources; **no number is invented**. This is the most important correctness test in the
   build — the failure mode is a fabricated expense ratio or lock-in period a user could act on.
9. **Citation-resolution resilience.** Force an invalid or missing `chunk_id` in the model output.
   **Expected:** fallback to the top-ranked chunk's URL, exactly one link still rendered, no crash.
10. **Output-guard fallback.** Force a guard failure. **Expected:** one retry with stricter
    instructions, then the templated extractive fallback — still with a citation (SC-4, SC-3).
11. **Refusals make no LLM call.** Re-run the 5 refusal questions with Groq logging enabled.
    **Expected:** zero LLM requests, fixed refusal text, educational link present.
12. **Secret hygiene at runtime.** **Expected:** the API key is read from `.env`, never logged, and
    a missing key produces a clear message rather than a traceback (SC-14).
13. **Separation check.** Trace the query path's imports. **Expected:** `loader` and the chunker are
    unreachable from it; the store is opened read-only.

### Gate — must be true before Phase 6
- [ ] Preflight refuses to serve on a missing store and on a model-id mismatch, and never
      auto-ingests.
- [ ] All 10 factual questions retrieve the correct chunk in top-k, confirmed by hand.
- [ ] `k` and the relevance threshold are calibrated from observed scores and frozen in config.
- [ ] All 10 factual answers are ≤ 3 sentences with exactly one real, resolvable citation and a
      source-derived freshness stamp.
- [ ] The no-fabrication test returns "not in my sources" with no invented number.
- [ ] Refusals trigger zero LLM calls; the output guard's retry and fallback paths are proven.
- [ ] Query-time code contains no ingestion imports.

---

## Phase 6 — UI

### Objective
Wrap the verified query pipeline in the minimal Streamlit interface required by PRD §10, and
produce the submission artefacts that the UI makes meaningful.

### Files to create or modify

| File | Action | Contents |
|---|---|---|
| `app.py` | modify | C9: welcome line, 3 example questions, disclaimer, chat thread, answer block with citation and freshness stamp, refusal / no-answer / PII states, startup preflight surfaced as a friendly message |
| `src/render.py` | modify | C17: Streamlit rendering of answer, one link, and the freshness stamp; link label = `source_title` + `section` |
| `DISCLAIMER.md` | create | The exact disclaimer snippet shown in the UI (PRD §11) |
| `SOURCES.md` | create | The 5 URLs, generated from `src/sources.py` so the delivered list cannot drift from the ingested corpus (PRD §9.8) |
| `samples/sample_qa.md` | create | 5–10 real queries with the assistant's actual answers and links (SC-15) |
| `README.md` | create | Setup steps, scope (AMC + schemes), known limits — including the loader outcome from 2.0 and the documented limitation of the corpus |
| `requirements.txt` | modify | Final pinned set, matching what the app actually imports |

### What the phase implements
- C9 and the Streamlit half of C17, covering all ten PRD §10 UI requirements: welcome line, 3
  example questions, `Facts-only. No investment advice.`, chat input/thread, answer block with one
  citation and `Last updated from sources:`, refusal state with educational link, PII state without
  echo, honest no-answer state, minimal visuals with no charts or returns tables, and no
  re-ingestion on load.

### Inputs and outputs
- **Inputs:** the verified pipeline from Phases 3–5; `GROQ_API_KEY`; the prebuilt store.
- **Outputs:** a running app plus README, SOURCES, DISCLAIMER, and the sample Q&A file.

### Verification (run in order)

1. **Start the app.** `streamlit run app.py`. **Expected:** the page loads with no re-ingestion —
   confirm the store's files are untouched and no ingestion stage logs appear (SC-10, SC-13).
2. **First-screen requirements.** **Expected:** welcome line, exactly 3 example questions, and the
   disclaimer reading `Facts-only. No investment advice.` are visible on load (SC-13).
3. **Example questions.** Click each of the 3. **Expected:** each is answered or refused
   appropriately, with no error and no empty answer.
4. **Answer block.** Inspect a factual answer. **Expected:** ≤ 3 sentences; **exactly one** link
   (count the links, not just check one exists); link label shows title + section; the link opens
   the registry page; `Last updated from sources:` is present and shows a source-derived date
   (SC-3, SC-4, SC-5).
5. **Refusal state.** Submit "Should I buy the HDFC Small Cap Fund?" **Expected:** the polite
   facts-only message plus a relevant educational link, and no recommendation anywhere in the
   answer (SC-6).
6. **Performance state.** Submit a returns/comparison question. **Expected:** a redirect to the
   official factsheet with no computed or compared figures (SC-7).
7. **PII state.** Submit a question containing a PAN-shaped value. **Expected:** a polite decline;
   the value does **not** appear in the thread; nothing is persisted (SC-8).
8. **No-answer state.** Submit an in-corpus but unanswerable question. **Expected:** an honest
   "not in my sources" response suggesting covered topics, and no invented fact.
9. **Session hygiene.** Close the session, reopen, and inspect the filesystem. **Expected:** no chat
   history carried over and no new files written under `data/`.
10. **Visual scope check.** **Expected:** no dashboards, charts, returns tables, or portfolio views
    (PRD §4, §10.9).
11. **Headless launch (Render readiness).** Start the app with the platform-style headless and
    `PORT` flags. **Expected:** it binds the given port and serves normally. Confirm the startup
    path never re-ingests, and that the prebuilt store and model weights are present in the
    deployable artifact (architecture §6).
12. **Full acceptance sweep.** Re-run and record the result of all 15 success criteria in PRD §7,
    including the secret-hygiene check (SC-14) and the sample Q&A (SC-15).

### Gate — the project is submission-ready
- [x] All ten PRD §10 UI requirements are visibly satisfied.
- [x] Every factual answer shows one link, ≤ 3 sentences, and a source-derived freshness stamp.
- [x] Refusal, performance, PII, and no-answer states all behave as specified.
- [x] Restarting the app performs no ingestion.
- [x] README, SOURCES.md, DISCLAIMER.md, and `samples/sample_qa.md` exist and match the running app.
- [x] All 15 PRD success criteria have a recorded pass result.

All six are asserted by `python -m src.verify_phase6`, which drives the real app through
`streamlit.testing.v1.AppTest`, makes live LLM calls, launches the server headlessly on a
platform-style `PORT`, and prints the SC-1..SC-15 table. It exits non-zero if any check fails or
any criterion is left without green evidence, so the boxes above are the suite's output rather
than a claim. The five corpus-gap questions are covered as honest no-answers, which is the
correct behaviour for a source set that does not contain those facts.

---

## Success-criteria traceability

| SC | Criterion | Established in |
|---|---|---|
| SC-1 | Scope limited to 5 URLs | Phase 1 (registry), Phase 2 (rejection test) |
| SC-2 | Grounded answers | Phase 2 (one fact per chunk), Phase 5 (traceability + no-fabrication), Phase 6 (as drawn by the UI) |
| SC-3 | One citation per answer | Phase 5 (resolution), Phase 6 (link count) |
| SC-4 | ≤ 3 sentences | Phase 4 (output check), Phase 5, Phase 6 |
| SC-5 | `Last updated from sources:` | Phase 2 (`fetched_at`), Phase 5 (source-derived) |
| SC-6 | Refusal behaviour | Phase 4 (intent), Phase 5 (no LLM call), Phase 6 |
| SC-7 | No performance claims | Phase 4 (intent), Phase 6 (factsheet redirect) |
| SC-8 | No PII | Phase 4 (block, no echo, no log), Phase 6 (state) |
| SC-9 | Public sources only | Phase 1 (registry), Phase 2 (containment) |
| SC-10 | Ingestion runs once | Phase 3 (idempotency), Phase 6 (restart check) |
| SC-11 | Inspectable chunks | Phase 2 (`chunks.txt` inspection) |
| SC-12 | Same embedding model | Phase 1 (local load), Phase 3 (round-trip), Phase 5 (preflight) |
| SC-13 | UI completeness | Phase 6 |
| SC-14 | Secret hygiene | Phase 1 (ignore rules), Phase 5 (runtime), Phase 6 (sweep) |
| SC-15 | Sample Q&A | Phase 6 |

## Deliverables map (PRD §11 — not a seventh phase)

| Deliverable | Produced in |
|---|---|
| Working prototype link, or ≤ 3-minute demo video | Phase 6 |
| Source list of the 5 URLs | Phase 6 (`SOURCES.md`, generated from the registry) |
| README with setup, scope, known limits | Phase 6 |
| Sample Q&A file (5–10 queries) | Phase 6 |
| Disclaimer snippet | Phase 6 (`DISCLAIMER.md`) |
| Chunk artefact | Phase 2 (`data/chunks.txt`) |
| Chunking strategy proposal | Phase 2 (`doc/chunking-strategy.md`) |

## Open items carried forward

Unresolved from PRD §"Open Questions" and architecture §2.1 — each must be settled by the phase
shown, not assumed:

1. **Source domain** (PRD open question 1) — whether the five supplied URLs are the accepted source
   list, or whether primary AMC/AMFI pages must also be ingested. Settled before Phase 2; if the
   registry changes, `corpus_hash` changes and Phase 3 re-ingests with `--force`.
2. **Scheme category labels** (PRD open question 2) — confirmed from the source pages during Phase
   2 inspection, never taken from the brief's labels.
3. **Hosting vs. demo video** (PRD open question 3) — decided at Phase 6, after the app is stable.
4. **Page rendering** (architecture §2.1) — classified in Phase 2.0 before the loader is written; if
   outcome C is reached, the additional dependency is justified in the README as forced by the data.
