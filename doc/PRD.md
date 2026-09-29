# PRD — Mutual Fund FAQ Assistant (Facts-Only RAG Chatbot)

**Source of truth:** `doc/ProblemStatement.txt`
**Status:** Draft for review · No implementation code included in this document.

---

## 1. Goal

Build a small, working **RAG (Retrieval-Augmented Generation) FAQ assistant** that answers
*factual* questions about mutual fund schemes using **only** a scoped corpus of official public
pages (AMC / SEBI / AMFI material such as factsheets, KIM/SID, scheme FAQs, fee & charges pages,
riskometer/benchmark notes, and statement/tax-document guides).

The assistant must:

- Answer only fact-based questions — e.g. expense ratio, exit load, minimum SIP, ELSS lock-in,
  riskometer, benchmark, and how to download statements.
- Attach **one clear source link in every answer**.
- **Refuse** opinionated/portfolio questions (e.g. "Should I buy/sell?") with a polite, facts-only
  message plus a relevant educational link.
- Never give investment advice and never make performance claims.

It follows a standard two-stage RAG pipeline:

| Stage | Flow |
|---|---|
| Ingestion (run once) | Load → Chunk → Embed → Store in Vector DB |
| Query (per question) | Question → Embed → Retrieve top-k chunks → LLM → Answer |

---

## 2. Target Users

1. **Retail users comparing schemes** — want quick, sourced facts (fees, loads, SIP amount, tax
   treatment) while evaluating schemes in the selected AMC.
2. **Support / content teams** answering repetitive mutual fund questions — want a consistent,
   citation-backed answer they can point a customer to.

Neither user group is asking for investment advice; the product deliberately does not serve that
need.

---

## 3. In-Scope Features

1. **Scoped corpus ingestion**
   - One AMC and **3–5 schemes** under it (five are fixed by the brief — see §5).
   - Collect public source pages per scheme: factsheet, KIM/SID, scheme FAQs, fee/charges pages,
     riskometer/benchmark notes, statement/tax-doc guides.
2. **Ingestion pipeline that persists**
   - Load → Chunk → Embed → Store in ChromaDB, persisted to disk so ingestion runs **once**, not on
     every restart.
3. **Chunk inspection artefact**
   - Save all chunks to a **readable `.txt` file** for manual inspection.
4. **Retrieval-augmented Q&A**
   - User question → embed with the same embedding model → retrieve top chunks → LLM generates a
     grounded answer.
5. **Mandatory single citation per answer** — one source link shown with every factual answer.
6. **Freshness marker** — every answer includes `Last updated from sources: <date/source stamp>`.
7. **Answer length limit** — answers kept to **≤ 3 sentences**.
8. **Refusal path** — opinionated/portfolio questions get a polite facts-only message plus a
   relevant educational link.
9. **PII guard** — do not accept or store PAN, Aadhaar, account numbers, OTPs, emails, phone
   numbers.
10. **No performance computation** — do not compute or compare returns; link to the official
    factsheet if asked.
11. **Minimal chat UI** — welcome line, 3 example questions, chat input/output, citation link,
    disclaimer note, refusal message.

---

## 4. Out-of-Scope Features

- Any investment advice, buy/sell/hold recommendations, or portfolio allocation guidance.
- Performance analysis: return calculation, return comparison, ranking, ratio maths, backtests.
- Advice on which scheme to choose, risk profiling, goal planning, or suitability assessment.
- Support for AMCs or schemes outside the scoped set in §5.
- Asset classes other than equity mutual funds (debt, hybrid products beyond the listed scheme,
  PMS, insurance, stocks, F&O, etc.).
- Live/real-time NAV, pricing, transactions, portfolio tracking, watchlists, statements of the
  user's own holdings.
- User accounts, login, authentication, chat history persistence across users.
- Collection or storage of any PII (see §8).
- Scraping of paywalled, private, or login-gated content.
- Use of third-party blogs, news portals, forums, or social media as sources.
- Screenshots or captures of the application back-end.
- Voice input, multi-language support, mobile apps, and analytics dashboards.

---

## 5. Selected AMC and Schemes

**AMC: HDFC Mutual Fund** (HDFC AMC), as fixed by the brief's source URLs.

Five schemes, all **Direct Growth** variants, as provided:

| # | Category label (per brief) | Scheme | Source URL |
|---|---|---|---|
| 1 | Large Cap | HDFC Large Cap Fund — Direct Growth | https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth |
| 2 | Flexi Cap | HDFC Equity Fund — Direct Growth | https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth |
| 3 | ELSS | HDFC ELSS Tax Saver Fund — Direct Plan Growth | https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth |
| 4 | Small Cap | HDFC Small Cap Fund — Direct Growth | https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth |
| 5 | Balanced Advantage (Hybrid) | HDFC Balanced Advantage Fund — Direct Growth | https://groww.in/mutual-funds/hdfc-balanced-advantage-fund-direct-growth |

The brief's own worked example is "one large-cap, one flexi-cap, one ELSS"; the delivered scope
covers that plus small cap and balanced advantage, staying inside the 3–5 scheme limit.

**Rule:** scheme facts (expense ratio, exit load, minimum SIP, lock-in, riskometer, benchmark,
minimum investment, etc.) are **never written into this PRD**. They must be ingested from the
source pages and cited at answer time. Any category label above is the label used in the brief and
must be confirmed against the source page during ingestion.

---

## 6. Example User Questions

**Answered (in scope, facts-only):**

1. What is the expense ratio of the HDFC Large Cap Fund Direct Growth?
2. What is the exit load on the HDFC Equity Fund Direct Growth?
3. What is the minimum SIP amount for the HDFC Small Cap Fund Direct Growth?
4. What is the lock-in period for the HDFC ELSS Tax Saver Fund Direct Plan Growth?
5. What is the riskometer level and benchmark of the HDFC Balanced Advantage Fund Direct Growth?
6. How do I download my capital-gains statement?
7. What are the management fees / other charges listed for the HDFC Large Cap Fund?
8. What is the minimum lump-sum investment amount for the HDFC ELSS Tax Saver Fund?
9. Where can I find the SID / KIM for the HDFC Equity Fund?
10. What documents are needed for tax reporting on these equity-oriented funds?

**Refused (out of scope, must return the polite facts-only message + educational link):**

- "Should I buy the HDFC Small Cap Fund?"
- "Is now a good time to sell my ELSS?"
- "Which of these five funds should be in my portfolio?"
- "Which fund has the best returns?"
- "Is HDFC Large Cap safe for a 3-year goal?"

**PII-handled (must be refused/not stored):**

- "My PAN is ABCDE1234F, what is the exit load?"
- "Update my account number 1234567890 / OTP / phone / email …"

---

## 7. Success Criteria

A milestone reviewer can verify each of the following:

| # | Criterion | Pass condition |
|---|---|---|
| SC-1 | Scope | Corpus is limited to HDFC AMC and the 5 schemes in §5; source list contains exactly the 5 supplied URLs. |
| SC-2 | Grounded answers | Every factual answer is composed from retrieved chunks; no answer introduces a fact absent from the retrieved source text. |
| SC-3 | Single citation | 100% of factual answers display exactly one source link. |
| SC-4 | Brevity | 100% of factual answers are ≤ 3 sentences. |
| SC-5 | Freshness stamp | 100% of answers include `Last updated from sources:`. |
| SC-6 | Refusal behaviour | Opinionated/portfolio questions return the polite facts-only message plus a relevant educational link, and contain no recommendation. |
| SC-7 | No performance claims | No answer computes, ranks, or compares returns; such questions redirect to the official factsheet. |
| SC-8 | No PII | PAN, Aadhaar, account number, OTP, email, and phone inputs are neither stored nor echoed. |
| SC-9 | Public sources only | All ingested pages are public AMC/SEBI/AMFI/regulator-distributor pages; no third-party blogs. No back-end screenshots. |
| SC-10 | Ingestion runs once | ChromaDB is persisted to disk; restarting the app does not re-ingest. |
| SC-11 | Inspectable chunks | A readable `.txt` file contains every chunk, with its metadata. |
| SC-12 | Embedding consistency | Chunks and questions are embedded with `all-MiniLM-L6-v2` (384-dim) via the same code path. |
| SC-13 | UI completeness | Welcome line + 3 example questions + "Facts-only. No investment advice." note are visible on load. |
| SC-14 | Secret hygiene | The Groq API key is read from `.env`, `.env` is git-ignored, and the key never appears in source or logs. |
| SC-15 | Sample Q&A | 5–10 example queries with the assistant's actual answers and links are provided. |

---

## 8. Constraints and Guardrails

**Content guardrails**
- **No advice.** The assistant states facts and cites them; it never recommends an action.
- **No performance claims.** No return computation or comparison; point to the official factsheet.
- **Brevity and clarity.** ≤ 3 sentences per answer, with `Last updated from sources:`.
- **One link, every answer.** No uncited factual output.

**Data guardrails**
- **Public sources only.** No back-end screenshots, no third-party blogs.
- **No PII.** Do not accept or store PAN, Aadhaar, account numbers, OTPs, emails, phone numbers.
  Detect such input, do not persist it, and respond with a polite redirect.
- **Scope discipline.** Only the selected AMC and schemes; out-of-scope questions are refused, not
  guessed.

**Operational guardrails**
- Ingestion is persisted; query-time work is retrieval + generation only.
- The refusal message and the disclaimer are fixed, reusable strings, and the disclaimer string
  delivered in §11 is the same one shown in the UI.
- Answers must fail closed: if retrieval returns nothing relevant, the assistant says it does not
  have the information in the sources instead of inventing one.

---

## 9. Source and Citation Requirements

1. **Only official public pages** are ingested: AMC pages and regulator (SEBI/AMFI) material —
   factsheets, KIM/SID, scheme FAQs, fee & charges pages, riskometer/benchmark notes, and
   statement/tax-document guides.
2. **No third-party blogs, news sites, forums, or social media** as sources.
3. **No back-end screenshots** in the submission.
4. **The five supplied URLs** form the source list delivered with the prototype.
5. **Citation rule:** every answer shows **one** clear citation link, resolved from the retrieved
   chunk's source metadata — never hand-written or guessed.
6. **Citation points to the page the fact came from**, not a generic homepage.
7. **Every chunk retains source metadata** (see §12) so a citation can always be produced.
8. **Provenance on record:** the source list (CSV/MD) is a deliverable and must match the ingested
   URLs.
9. **Answers that need a document (e.g. a return figure) redirect to the official factsheet**
   rather than stating numbers.

---

## 10. UI Requirements

Tiny, single-purpose interface — the brief asks for a minimal chat surface.

1. **Welcome line** on load.
2. **Three example questions** shown as clickable suggestions on load.
3. **Disclaimer note:** `Facts-only. No investment advice.` (exact wording from the brief; the
   same snippet is delivered as part of the submission).
4. **Chat input + message thread** for question and answer.
5. **Answer block** shows: the ≤ 3-sentence answer, **one citation link**, and
   `Last updated from sources: …`.
6. **Refusal state** for opinionated/portfolio questions: polite facts-only message + a relevant
   educational link.
7. **PII state:** if the input contains PAN/Aadhaar/account number/OTP/email/phone, the assistant
   declines and does not display or store the sensitive value.
8. **No-answer state:** honest "not available in my sources" message when retrieval finds nothing
   relevant.
9. Minimal visual design; no dashboards, charts, returns tables, or portfolio views.
10. The app must run against the persisted vector store — no re-ingestion on restart.

---

## 11. Deliverables

1. **Working prototype** — app or notebook link; if hosting is not possible, a **≤ 3-minute demo
   video**.
2. **Source list** (CSV or MD) of the **5 URLs** used.
3. **README** containing setup steps, scope (AMC + schemes), and known limits.
4. **Sample Q&A file** — 5–10 queries with the assistant's answers and links.
5. **Disclaimer snippet** used in the UI (facts-only, no advice).
6. **Chunk artefact** — all chunks in a readable `.txt` file for inspection.
7. **PRD** (this document).

Architecture artefacts referenced in the brief (chunking proposal, vector store layout) are
produced as part of the implementation stage, not in this PRD.

---

## 12. Technical Constraints

| Area | Constraint |
|---|---|
| Architecture | Two-stage RAG: **Ingestion** = Load → Chunk → Embed → Store; **Query** = Question → Embed → Retrieve top chunks → LLM → Answer. |
| Embedding model | `sentence-transformers/all-MiniLM-L6-v2` — runs locally, no API key, **384-dimension** vectors. The **same model** embeds both chunks and the user question. |
| Chunking strategy | **Decided by the AI agent (Cursor, OpenCode, or Claude Code) after inspecting the data.** Before writing code it must: inspect the data, **propose** a strategy, **justify** why it suits this data, and **specify** chunk size, overlap, and the metadata each chunk keeps. |
| Chunk artefact | All chunks written to a **readable `.txt` file** so they can be inspected. |
| Vector DB | **ChromaDB**, **persisted to disk**, so ingestion runs once and not on every restart. |
| LLM | **Groq**. |
| Secrets | API key stored in **`.env`**, **never committed to Git** (`.env` git-ignored, `.env.example` provided). |
| Sources | Public AMC/SEBI/AMFI pages only; no back-end screenshots; no third-party blogs. |
| PII | Never accept or store PAN, Aadhaar, account numbers, OTPs, emails, phone numbers. |
| Output rules | One citation link per answer; ≤ 3 sentences; `Last updated from sources:`; no performance claims. |

---

## Open Questions to Confirm Before Implementation

These follow from ambiguity in the brief itself; they are not assumptions invented here.

1. **Source domain.** The five supplied URLs are on `groww.in` (a distributor platform), while the
   brief also names "AMC/SEBI/AMFI" pages. Confirm whether the five supplied URLs are the accepted
   source list, or whether primary AMC/AMFI pages for the same five schemes must also be ingested.
2. **Category labels.** Confirm each scheme's official category from the source page during
   ingestion rather than relying on the brief's labels (§5).
3. **Hosting.** Prototype link vs. ≤ 3-minute demo video — decide before the submission date.
