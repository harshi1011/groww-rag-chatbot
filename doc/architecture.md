# Architecture — Mutual Fund FAQ Assistant (Facts-Only RAG Chatbot)

**Derived from:** `doc/ProblemStatement.txt` and `doc/PRD.md`
**Status:** Design document · **No application code in this document.**
**Scope note:** the chunking strategy is **deliberately not decided here** (§2.3). Only the
*contract* that a chunking strategy must satisfy is defined.

---

## 1. System components

The system is two pipelines that share exactly three things: the source registry, the embedder,
and the on-disk vector store. They do not share execution.

### 1.1 Offline components (ingestion — run once)

| # | Component | Responsibility |
|---|---|---|
| C1 | **Source registry** | Declarative list of the 5 approved URLs + expected scheme name and document type. Single place where "what is in scope" is declared. |
| C2 | **Fetcher / Loader** | Retrieves each public page, extracts readable text, records retrieval time and final URL. |
| C3 | **Normalizer** | Strips nav/boilerplate noise, collapses whitespace, normalizes headings — so chunks carry facts, not chrome. |
| C4 | **Chunker** *(strategy TBD)* | Splits normalized text into retrievable units and attaches metadata. |
| C5 | **Chunk dump writer** | Writes every chunk + metadata to a human-readable `.txt` file (PRD deliverable / SC-11). |
| C6 | **Embedder** *(shared)* | Encodes text with `all-MiniLM-L6-v2` → 384-dim vectors. Loaded once, used by both pipelines. |
| C7 | **Vector store writer** | Writes chunks + vectors + metadata into a persistent ChromaDB directory. |
| C8 | **Manifest writer** | Emits `manifest.json`: corpus hash, source list, chunking config, embedding model id, dimension, corpus count, ingestion timestamp. |

### 1.2 Online components (query — per question)

| # | Component | Responsibility |
|---|---|---|
| C9 | **Streamlit UI** | Welcome line, 3 example questions, disclaimer, chat thread, citation link, `Last updated from sources:`. |
| C10 | **Input guard (PII)** | Regex/heuristic screen before any model call; blocks and never persists sensitive input. |
| C11 | **Intent guard** | Classifies the question as `factual` / `advice` / `performance` / `off-topic` / `out-of-scope-scheme`. |
| C12 | **Retriever** | Embeds the question, queries ChromaDB for top-k chunks with metadata. |
| C13 | **Relevance gate** | Applies a score threshold; below-threshold → *unanswered* path, no LLM call. |
| C14 | **Prompt builder** | Assembles system prompt (rules) + grounded context + question + output contract. |
| C15 | **LLM client (Groq)** | Generates the ≤ 3-sentence answer and names the supporting chunk id. |
| C16 | **Output guard** | Enforces citation present, sentence count ≤ 3, no advice/performance language; falls back or refuses. |
| C17 | **Answer renderer** | Renders answer + one citation link + `Last updated from sources:` or a refusal message. |
| C18 | **Config / secrets** | `.env` loader for the Groq key; model id, k, thresholds, refusal/disclaimer strings in config. |

### 1.3 Explicitly not included

| Rejected | Why |
|---|---|
| Orchestrator frameworks (LangChain / LlamaIndex) | The brief prescribes the exact four-step pipelines. A framework would hide those steps behind abstractions, and every extra dependency must be justified in the README. |
| Guardrail libraries (e.g. NeMo Guardrails) | Guardrails here are a handful of deterministic checks. A framework is disproportionate and would obscure the citation contract. |
| A hosted vector database | The brief specifies ChromaDB persisted to disk, and a local prototype needs no network for retrieval. |
| Hybrid/BM25 search, rerankers, query rewriting | Unnecessary for a ~5-page corpus; retrieval quality is validated by inspecting `chunks.txt` and the top-k output directly. |
| A chat-history database, user accounts, logging database | Out of scope per PRD §4. Stdout logging only. |
| A separate embedding service | `all-MiniLM-L6-v2` runs in-process, so a service would add latency for no benefit. |

---

## 2. Data ingestion flow — Load → Chunk → Embed → Store

Runs **once**, via an explicit command, never at app startup.

```
sources registry (5 URLs)
        │
        ▼
  [1] LOAD ────────► [2] CHUNK ──────► [3] EMBED ──────► [4] STORE
      │                 │                 │                │
      │                 │                 │                ├─► ./data/chroma  (persistent dir)
      │                 │                 │                └─► manifest.json
      │                 │                 │                      corpus hash, model id,
      │                 │                 │                      chunking config, counts
      │                 │                 │
      │                 └─► chunks.txt    └─► same Embedder instance / model id
      │                     (readable dump)
      └─► normalized text + source_url, scheme, doc_type, fetched_at
```

### 2.1 Load
- Read URLs from the source registry; only those 5 are fetched (SC-1, SC-9).
- Record per source: `source_url`, `scheme_name`, `doc_type`, `fetched_at`, final resolved URL,
  and a content hash for the manifest.
- **Open technical check before implementation:** confirm whether these pages return their text in
  the initial HTML. If the content is rendered client-side, plain HTTP fetch yields no facts and
  the Load step needs a rendering-capable fetch. This is verified by inspecting the data, not
  assumed.

### 2.2 Normalizer
- Remove navigation, footers, cookie banners, and repeated boilerplate.
- Keep section headings — they are the most reliable citation labels available.

### 2.3 Chunk — **strategy intentionally undecided**

The PRD and the brief require the implementing agent to inspect the real data first, then propose
a strategy with a written justification. This document therefore fixes only the **contract**:

**A chunking strategy must produce chunks that:**

| Requirement | Rationale |
|---|---|
| Keep a single fact or a single labelled section intact | Answer length is capped at ≤ 3 sentences, so a chunk spanning several unrelated facts invites multi-fact answers. |
| Carry a resolvable citation | Every chunk must map to exactly one source URL (PRD §9). |
| Carry a human-readable section label | The citation shown to a user is the page + section, not a raw offset. |
| Not split mid-number | Fee percentages, SIP amounts, and lock-in periods must never be cut in half. |

**The strategy must specify, in writing, before code is written:** chunking unit (heading / block
/ sentence), chunk size, overlap, and the metadata each chunk keeps. That write-up is a required
step in the build sequence, not a decision made in this document.

### 2.4 Minimum metadata contract

Whatever strategy is chosen, every chunk must carry at least these fields. Additional fields are
the chunker's discretion; the fields below are mandatory because citations and the freshness stamp
depend on them.

| Field | Used for |
|---|---|
| `chunk_id` | Stable id so the LLM can name the chunk it used. |
| `source_url` | **The citation link.** |
| `source_title` | Link label in the UI. |
| `scheme_name` | Scope and answer precision. |
| `doc_type` | factsheet / KIM / SID / FAQ / fees / riskometer / statement-guide. |
| `section` | Human-readable section name. |
| `chunk_index` | Ordering and debugging. |
| `fetched_at` | Drives `Last updated from sources:`. |
| `content_hash` | Detects drift; supports idempotent re-ingest. |

### 2.5 Embed
- Every chunk is embedded by the shared embedder (C6) in a single batch pass.
- `normalize_embeddings` is fixed to one value and used identically in both pipelines (§10).
- Vector dimension is asserted to be 384; a mismatch aborts ingestion rather than writing bad
  vectors.

### 2.6 Store
- Chunks are written to a persistent ChromaDB directory (`./data/chroma`) with cosine distance
  configured at collection creation.
- If the directory already exists and the manifest hash matches, ingestion is a no-op
  ("already ingested, nothing to do") — this is the mechanism behind SC-10.
- Re-ingesting a changed corpus requires an explicit `--force` flag so state is never silently
  replaced.

---

## 3. Data retrieval flow — Question → Embed → Retrieve → LLM → Answer

Runs per question, inside the Streamlit app. **No page fetching, no chunking, no embedding-model
download of new documents here** — the store is already built (§9).

```
user question
     │
     ▼
 [PII guard] ──blocked──► refuse (no store, no echo)
     │ pass
     ▼
 [Intent guard] ──advice / performance / off-topic──► refusal message + educational link
     │ factual
     ▼
 [Embed question]  (same model, same normalization)
     │
     ▼
 [Chroma query top-k]  ──►  (chunk text + metadata, with scores)
     │
     ▼
 [Relevance gate] ──below threshold / empty──► "not in my sources" + nearest available topics
     │ pass
     ▼
 [Prompt builder]  system rules + context chunks + question + output contract
     │
     ▼
 [Groq LLM]  →  ≤ 3 sentences + supporting chunk id
     │
     ▼
 [Output guard] ──fails──► retry once, then fall back to a templated extractive answer
     │ pass
     ▼
 [Renderer]  answer + ONE citation link + "Last updated from sources: <fetched_at>"
```

### 3.1 Retrieval parameters (to be tuned during build, not fixed here)
- `k`: small corpus, so a low single-digit `k` (3–5) is the starting point; final value chosen
  after inspecting top-k output against the sample Q&A.
- Distance threshold: set from observed score distribution on the sample questions, then frozen in
  config. A threshold is what makes "unanswered" detectable instead of answered from the nearest
  irrelevant chunk.

### 3.2 Prompt contract given to the LLM
- Role: a facts-only assistant for the selected AMC's schemes.
- Answer **only** from the supplied context; if the context lacks the fact, say so.
- Maximum three sentences; no tables, no lists, no calculations.
- Never recommend, rank, or predict; no return figures or comparisons.
- Return the supporting `chunk_id` explicitly, on its own line, so the citation is resolved from
  metadata rather than generated by the model.

---

## 4. How citations and source metadata flow through the system

**Principle: the LLM never writes a URL. Citations are resolved from stored metadata.**

```
Page URL ──► Loader ──► chunk.metadata.source_url ──► Chroma record
                                                        │
user question ──► retrieve top-k ──► chunk text + metadata returned
                                                │
                              LLM names the chunk_id it used
                                                │
                     chunk_id ──► metadata.source_url ──► ONE link in the UI
```

1. **Ingestion** attaches `source_url` to every chunk at write time (SC-11 inspectable in
   `chunks.txt`).
2. **Retrieval** returns that metadata alongside every chunk, so the mapping id → URL always
   travels with the text.
3. **Generation** returns only a `chunk_id`. This keeps URL fabrication structurally impossible.
4. **Resolution** maps that id to `source_url` in application code. If the LLM omits or invents an
   id, the system falls back to the top-ranked chunk's URL.
5. **Exactly one link** is rendered per answer. If several supporting chunks share a URL, one link
   is shown. If they do not, the top-ranked chunk's URL is shown and the answer is rewritten to
   stay within a single source — the output guard rejects cross-source claims.
6. **Link label** is `source_title` + `section`, so a reader knows what they are opening.
7. **`Last updated from sources:`** is derived from the chunk's `fetched_at` / source-published
   date. It reflects **source data freshness, not the current chat time** — showing today's date
   would falsely imply the underlying fact is current.
8. **Provenance** for the deliverable source list is generated from the same registry the app
   ingests, so the delivered list and the ingested corpus cannot drift apart.

---

## 5. Guardrails

Guardrails are ordered by cost: the cheapest deterministic check that can stop a request runs
first, so blocked requests never reach the LLM.

| Order | Guard | Check | On trigger |
|---|---|---|---|
| 1 | **PII (C10)** | PAN-shaped, Aadhaar-shaped, long digit runs, OTP keywords, email, phone patterns | Decline politely; do not echo the value; do not write to session state or logs |
| 2 | **Intent (C11)** | Opinion verbs ("should I", "buy", "sell", "best", "recommend"), return/comparison terms, other AMC names or other schemes | Return the fixed refusal message + a relevant educational link; no LLM call |
| 3 | **Relevance (C13)** | Top-1 score below the configured threshold, or empty result set | "This isn't in my sources" + the topics that are covered |
| 4 | **Grounding (C14)** | Prompt forbids answering outside supplied context | Model states it lacks the information |
| 5 | **Output (C16)** | Citation present and resolvable; ≤ 3 sentences; no advice/performance phrasing | Retry once with stricter instructions, then fall back to a templated extractive answer from the top chunk |

### 5.1 Investment advice
- Detected **before** generation by the intent guard, so a refusal cannot be contaminated by
  partial context.
- The refusal message is a fixed string (no generation) — consistent tone, and it cannot itself
  drift into advice.
- **Performance claims are a sub-case of the same guard:** "which fund has the best returns",
  "compare these funds", "what will this return" route to the *no-performance* response, which
  points to the official factsheet rather than quoting numbers (PRD §9.9).
- The system prompt bans recommendations explicitly, and the output guard scans the generated
  text for recommendation phrasing as a second line of defense.
- Educational links used in refusals are fixed, curated links recorded in config — never
  LLM-generated URLs.

### 5.2 Off-topic questions
- Two distinct cases, treated differently:
  - **In-corpus but unanswerable** (e.g. an obscure fee line absent from the sources) → relevance
    gate → "not in my sources" response. Honest, not a refusal.
  - **Out-of-scope** (other AMCs, other asset classes, unrelated topics) → intent guard →
    scope message naming what the assistant does cover.
- Scope is enforced by the corpus itself: only the 5 schemes are in Chroma, so off-scope retrieval
  cannot succeed even if the guard misses it.

### 5.3 PII
- Screening happens on **raw user input, before embedding** — the question is never converted to a
  vector if it contains PII, and never leaves the process.
- No persistent storage of any kind: chat history lives in Streamlit session state only and is
  discarded on session end.
- Logging is stdout-only and question text is **not** logged, so PII cannot be captured in logs
  (SC-8, SC-14).
- Refusal text never repeats the detected value back to the user.

### 5.4 Unanswered questions
- Three sources of "no answer", all intentional and all distinct from a refusal:
  1. No chunk above the relevance threshold.
  2. Context retrieved but does not contain the fact → the model is instructed to say so.
  3. LLM output fails the output guard after one retry.
- The system **fails closed**: it never falls back to parametric knowledge. This is the single
  most important correctness property, since the alternative failure mode is a fabricated
  expense ratio or a fabricated lock-in period — both of which a user could act on.
- "Not in my sources" responses suggest the topics the assistant does cover, keeping the
  conversation useful without guessing.

---

## 6. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Language | **Python 3** | Single language across ingestion, retrieval, and UI. |
| UI | **Streamlit** | Required by the brief; `st.chat_message` / `st.chat_input` map 1:1 onto the PRD §10 UI requirements. |
| Embeddings | **sentence-transformers / all-MiniLM-L6-v2** | Local, no API key, 384-dim. Same model for chunks and questions (§10). |
| Vector store | **ChromaDB**, persistent client | On-disk directory; cosine distance fixed at collection creation. |
| LLM | **Groq** | Key from `.env`; `.env` git-ignored, `.env.example` provided. |
| HTTP for fetching | **Python standard library `urllib`** (or `requests` if already present) | Only needed by the Load step. Chosen to avoid a dependency for 5 URLs. Revisit only if the page-rendering check in §2.1 requires more. |
| PII detection | **Python `re`** | Fixed patterns for PAN / Aadhaar / digits / email / phone. No ML needed. |
| Config | **`.env` + a small config module** | No config framework; a few constants read from one place. |
| Logging | **`logging` to stdout** | No database, no analytics, no back-end screenshots (SC-9). |

**Deployment note (Render).** The architecture is deployable without changing the design, with two
things handled at build time rather than runtime:

1. **Ephemeral filesystem.** Render's filesystem does not persist across restarts, so the ChromaDB
   directory cannot be assumed to survive. The workable approach is to ship the prebuilt store
   **inside the deployable artifact** (committed under `data/chroma`, or produced by a build
   command) and open it read-only at startup. This preserves the "ingestion runs once" property —
   the corpus is built once, offline, and shipped. The alternative (re-ingest on every cold start)
   is rejected because it needs network access at runtime and contradicts the brief.
2. **Model weights.** `all-MiniLM-L6-v2` must already be present in the artifact or cache at
   startup; pre-fetch it during build so query latency and network availability are not concerns.
3. **Streamlit flags** run headless on the platform-assigned `PORT`; these are launch-time config,
   not code changes.

---

## 7. Proposed project folder structure

Proposed only — **nothing is created at this stage.**

```
groww-rag-chatbot/
├── doc/
│   ├── ProblemStatement.txt
│   ├── PRD.md
│   ├── architecture.md              # this document
│   └── chunking-strategy.md         # required: data inspection + proposal (§2.3)
│
├── app.py                           # Streamlit entry point (query-time only)
├── ingest.py                        # ingestion entry point (offline, run once)
│
├── src/
│   ├── config.py                    # paths, model id, k, thresholds, fixed strings
│   ├── sources.py                   # the 5-URL source registry
│   ├── loader.py                    # fetch + normalize + per-source metadata
│   ├── chunker.py                   # strategy loaded from config; the strategy itself is TBD
│   ├── embedder.py                  # the single shared embedding code path
│   ├── vectorstore.py               # Chroma open/write/query helpers
│   ├── retrieval.py                 # embed query, top-k, relevance gate
│   ├── prompt.py                    # system rules + context assembly + output contract
│   ├── llm.py                       # Groq client
│   ├── guardrails.py                # PII, intent, output checks, refusal strings
│   └── render.py                    # answer + citation + freshness stamp rendering
│
├── data/                            # generated, not hand-edited
│   ├── chroma/                      # persistent vector store (built once)
│   ├── chunks.txt                   # readable chunk dump (SC-11)
│   └── manifest.json                # corpus hash, model id, chunking config, counts
│
├── requirements.txt
├── .env.example                     # GROQ_API_KEY placeholder
├── .gitignore                       # .env, data/chroma, caches
├── README.md                        # setup, scope, known limits
├── SOURCES.md                       # the 5 URLs (deliverable)
├── DISCLAIMER.md                    # disclaimer snippet (deliverable)
└── samples/
    └── sample_qa.md                 # 5–10 queries with real answers + links (SC-15)
```

**Why this shape:** `ingest.py` and `app.py` are two entry points over a shared `src/` package, so
the separation in §9 is enforced by the file layout. `data/` is entirely generated and git-ignored,
keeping derived artefacts from being mistaken for source. `doc/chunking-strategy.md` exists so the
required proposal is a tracked, reviewable artefact rather than an implicit decision.

---

## 8. Architecture diagram

```
 ┌───────────────────────────────────────────────────────────────────────────────┐
 │  OFFLINE — ingest.py   (run manually, once; never at app startup)              │
 └───────────────────────────────────────────────────────────────────────────────┘

   sources.py             loader.py          chunker.py*        embedder.py
  ┌──────────┐          ┌──────────┐       ┌──────────┐       ┌──────────┐
  │ 5 URLs   │─────────►│ fetch +  │──────►│ strategy │──────►│ all-Mini │
  │ + scheme │          │ normalise│       │  (TBD)   │       │ LM-L6-v2 │
  │ + type   │          └────┬─────┘       └────┬─────┘       │ 384-dim  │
  └──────────┘               │                  │             └────┬─────┘
                             │                  │                  │
                       text + source       chunk + metadata        │ vectors
                             │            (source_url, title,      │
                             │             scheme, section, …)     │
                             └──────────────┬───────────────────────┘
                                            │
                              ┌─────────────┴──────────────┐
                              ▼                            ▼
                      ┌───────────────┐          ┌──────────────────┐
                      │  chunks.txt   │          │  ChromaDB  ──────┼──► data/chroma
                      │ (readable)    │          │  (cosine, persist)│    manifest.json
                      └───────────────┘          └──────────────────┘


 ══════════════════ offline artefact boundary — ingest.py ends here ══════════════════


 ┌───────────────────────────────────────────────────────────────────────────────┐
 │  ONLINE — app.py  (Streamlit; per question; opens store read-only)             │
 └───────────────────────────────────────────────────────────────────────────────┘

  ┌──────────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌─────────┐
  │ Streamlit UI │──►│  PII     │──►│  Intent  │──►│ Embedder │──►│ Chroma  │
  │ welcome + 3  │   │  guard   │   │  guard   │   │ (SAME    │   │ top-k + │
  │ examples +   │   └────┬─────┘   └────┬─────┘   │  model)  │   │ metadata│
  │ disclaimer   │        │             │          └──────────┘   └────┬────┘
  └──────────────┘        │ blocked     │ advice/performance/        │ chunks
                          ▼             ▼ off-topic                  │ + scores
                   ┌────────────┐  refusal msg +                    │
                   │  REFUSAL   │  educational link                  ▼
                   │  (fixed    │                              ┌──────────┐
                   │   string)  │                              │Relevance │
                   └────────────┘                              │  gate    │
                                                                └────┬─────┘
                              no answer ◄────────────────────────────┤ pass
                                                                     ▼
                                                              ┌───────────┐
                                                              │  prompt   │ rules +
                                                              │  builder  │ context
                                                              └─────┬─────┘
                                                                     ▼
                            ┌───────────────┐   answer + chunk_id  ┌──────────┐
                            │    Streamlit  │◄───────────────────────│   Groq   │
                            │    renderer   │                        │  (LLM)   │
                            └───────┬───────┘                        └────┬─────┘
                                    │                                     │
                          ┌─────────┴──────────┐   ≤3 sentences + resolvable citation
                          │   Output guard     │◄─────────────────────────┘
                          │ (fallback if fail) │
                          └─────────┬──────────┘
                                    ▼
              ANSWER  +  ONE citation link  +  "Last updated from sources: <fetched_at>"
```

---

## 9. How ingestion and query-time retrieval are separated

The brief's core operational requirement is that **ingestion runs once and not on every
restart** (SC-10). Separation is enforced at four levels.

| Level | Mechanism |
|---|---|
| **Entry points** | `ingest.py` and `app.py` are separate executables. The app contains no fetch, chunk, or write-store code path, and ingestion is never triggered implicitly. |
| **Imports** | `app.py` imports retrieval, prompt, llm, guardrails, render, and the *read-only* half of `vectorstore`. It does not import `loader` or the chunk-writing path. A stray ingestion import is therefore visible in review. |
| **Artefact boundary** | The handoff is the on-disk directory: `data/chroma/` + `data/manifest.json` + `data/chunks.txt`. Nothing passes through memory or a network call between the two pipelines. |
| **Store access mode** | The app opens the Chroma directory read-only. If the directory is missing, the app states that ingestion has not been run and tells the user the command — it never silently ingests. |

**Startup preflight (query-time only, cheap):**
1. Does `data/chroma/` exist? → if not, stop with "run ingestion first".
2. Does `manifest.json` exist? → if not, stop.
3. Does the manifest's `embedding_model_id` equal the configured model id? → if not, **refuse to
   serve** and require re-ingestion. This is the runtime half of the consistency guarantee in §10.
4. Does the manifest's `corpus_hash` match the source registry? → if not, warn that sources changed
   and re-ingestion is needed.

**Cost profile.** Ingestion is a minutes-long offline job with a one-time model load. Query time
does only: guard checks (µs) → one question embedding (~ms) → one vector search (~ms) → one Groq
call (~1s). On a Render free instance, the dominant per-request cost is the Groq call; the local
embedding and Chroma search are negligible against it.

---

## 10. How the same embedding model is used for documents and questions

A silent mismatch between ingestion-time and query-time embeddings is the classic cause of "the
bot ignores my corpus" — and it is invisible until answers are wrong. Four mechanisms prevent it.

### 10.1 One code path
- A single `embedder.py` module owns the `SentenceTransformer` instance.
- Both pipelines call the same `embed()` function. There is no second place in the codebase where
  a model is constructed — loading the model twice, or in two places, is a review-checkable
  violation.
- The model is instantiated once per process and reused. Ingestion batches all chunk embeddings
  through one call; the app embeds one question at a time through the same call.

### 10.2 Pinned, identical configuration
Configuration is centralised and applied on both paths, including the settings that are easy to
forget and silently break comparability:

| Setting | Pinned value / rule | Why it matters |
|---|---|---|
| Model id | `sentence-transformers/all-MiniLM-L6-v2` | Must be one string, from config, not a literal in two files. |
| Dimension | 384, asserted at both write and query time | A mismatch means the wrong model; fail loudly. |
| `normalize_embeddings` | One fixed value used on both paths | If chunks are normalised and questions are not, cosine distances are not comparable. |
| Max sequence length | Left at the model default, applied on both paths | Truncation rules must match or long chunks embed differently than they are read. |
| Prompt prefix | None, on both paths | MiniLM is not an instruction model; adding a prefix on one path only would break comparability. |
| Distance metric | `cosine`, fixed in collection metadata at creation | Must be set identically when writing and when querying. |

### 10.3 Persisted provenance
- `manifest.json` records the model id, dimension, and normalisation flag used at ingestion.
- The Chroma collection metadata also records the model id.
- The app asserts the configured model id equals the manifest's before serving (§9 preflight). On
  mismatch it refuses to start and reports the re-ingestion command.

### 10.4 Verification before trusting answers
- A post-ingestion check embeds a sample of stored chunk texts again and compares against the
  stored vectors; an unexplained mismatch fails the build rather than surfacing later as bad
  answers.
- The sample Q&A file (SC-15) is the end-to-end confirmation: if questions retrieve the right
  chunks, the vectors are comparable.

---

## 11. Build sequence (so decisions land in the right order)

1. Fetch and inspect the 5 source pages; confirm the Load approach (§2.1 check).
2. Write `doc/chunking-strategy.md` — inspection findings, proposed strategy, justification, chunk
   size, overlap, metadata. **No chunking code before this exists.**
3. Implement ingestion to the §2 contract; produce `chunks.txt`, `data/chroma`, `manifest.json`.
4. Inspect `chunks.txt` by hand and fix the strategy if facts are split or merged badly.
5. Implement the query path and the five guardrails.
6. Build the Streamlit UI against PRD §10.
7. Tune `k` and the relevance threshold against the sample questions; freeze in config.
8. Produce the deliverables (README, SOURCES, DISCLAIMER, sample Q&A, demo video or hosted link).
