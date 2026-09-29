# Sample Q&A

Ten questions with the answers this app actually produced. Captured from a live
run of the delivered app on **29 September 2026**, against the store ingested on
**28 September 2026** (`data/manifest.json`). The answers are transcribed
verbatim, not paraphrased, and the links are the ones the app rendered.

Every entry is one of the app's real states, so this file doubles as
demonstration of the behaviour required by PRD §10: a factual answer, a
refusal, a PII block, an honest no-answer, and a follow-up resolved against the
previous turn.

Reproduce any row by pasting the question into the app. The numbers depend on
what the source pages said on the ingestion date, so re-running ingestion later
may change them; that is the point of the `Last updated from sources:` line.

---

## 1. Factual answer — expense ratio

> **What is the expense ratio of the HDFC Large Cap Fund Direct Growth?**

The expense ratio is 1.03%.

Source: [HDFC Large Cap Fund Direct Growth — Key facts](https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth)

*Last updated from sources: 28 Sep 2026*

One sentence, one link, the link label names the page and the section the fact
was read from.

---

## 2. Factual answer — exit load

> **What is the exit load on the HDFC Equity Fund Direct Growth?**

The exit load is 1% if redeemed within 1 year.

Source: [HDFC Flexi Cap Direct Plan Growth — Exit Load](https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth)

*Last updated from sources: 28 Sep 2026*

Note the link label uses the name the page itself carries
(`HDFC Flexi Cap Direct Plan Growth`), not the brief's `HDFC Equity Fund`. The
question is asked in the brief's name and is understood correctly; see the
naming note in `SOURCES.md`.

---

## 3. Factual answer — minimum SIP

> **What is the minimum SIP amount for the HDFC Small Cap Fund Direct Growth?**

The minimum SIP amount for the HDFC Small Cap Fund Direct Growth is ₹100.

Source: [HDFC Small Cap Fund Direct Growth — Key facts](https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth)

*Last updated from sources: 28 Sep 2026*

---

## 4. Factual answer — ELSS lock-in

> **What is the lock-in period for the HDFC ELSS Tax Saver Fund Direct Plan Growth?**

The lock-in period for the HDFC ELSS Tax Saver Fund Direct Plan Growth is
3 years.

Source: [HDFC ELSS Tax Saver Fund Direct Plan Growth — Key facts](https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth)

*Last updated from sources: 28 Sep 2026*

---

## 5. Factual answer — riskometer and benchmark, two facts in two sentences

> **What is the riskometer level and benchmark of the HDFC Balanced Advantage Fund Direct Growth?**

The fund is classified as Very High Risk and its benchmark is the NIFTY 50 Hybrid
Composite Debt 50:50 Index.

Source: [HDFC Balanced Advantage Fund Direct Growth — Key facts](https://groww.in/mutual-funds/hdfc-balanced-advantage-fund-direct-growth)

*Last updated from sources: 28 Sep 2026*

---

## 6. Refusal — a recommendation question

> **Should I buy the HDFC Small Cap Fund?**

I only share facts from the official source pages, so I cannot advise on whether
to buy, sell, or hold a scheme. You can read the scheme's own details and risk
disclosures on the official page, and speak to a registered mutual fund
distributor if you would like a view on suitability.

[Scheme details and risk disclosures](https://www.hdfcmf.com/)

No scheme was retrieved and no language model was called. The link is a fixed
educational resource, not a citation, so it carries no `Last updated` line — the
app never read that page.

---

## 7. Refusal — a performance question

> **Which fund has the best returns?**

I do not calculate, compare, or quote returns. For performance figures, please
refer to the official factsheet published by the AMC.

[Official factsheets](https://www.hdfcmf.com/)

No fund is named and no figure is produced. The redirect is the answer.

---

## 8. PII block

> **My PAN is ABCDE1234F, what is the exit load?**

For your security, please do not share personal identifiers such as PAN,
Aadhaar, account numbers, OTPs, email addresses, or phone numbers. I do not
store them. Please ask your question again without personal details.

The value `ABCDE1234F` is quoted above only because this file documents the
behaviour. In the app it appears nowhere: the thread draws the withheld
placeholder instead of the typed text, the conversation buffer records the same
placeholder, and the block happens before retrieval, so the store is never
touched.

---

## 9. Honest no-answer — a fact the corpus does not carry

> **What is the Sharpe ratio of HDFC Large Cap Fund Direct Growth?**

I do not have that in my sources. I can answer questions about expense ratio,
exit load, minimum SIP and investment amounts, ELSS lock-in, riskometer,
benchmark, and how to download statements and tax documents, for the five HDFC
Mutual Fund schemes in my sources. The closest topics in my sources are: HDFC
Large Cap Fund Direct Growth, HDFC Small Cap Fund Direct Growth.

Nothing is invented and no number is offered. The question is in scope and
sounds answerable, which is exactly the case a system that guesses would get
wrong.

---

## 10. Follow-up resolved against the previous turn

A second turn in the same conversation as entry 1, whose question and answer are
already recorded above. Only the new turn is transcribed here.

**Turn 2**, after entry 1's expense-ratio question:

> **What about its exit load?**

The exit load is 1% if redeemed within 1 year.

Source: [HDFC Large Cap Fund Direct Growth — Exit Load](https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth)

*Last updated from sources: 28 Sep 2026*

"Its" carries no scheme signal, so this turn is restated before retrieval. The
app shows what it interpreted:

> Follow-up understood as: What is the exit load of HDFC Large Cap Fund Direct Growth?

The rewriter may only add what the pronoun stood for. It never sees the corpus
and never writes an answer, and any failure — no key, timeout, too long a
reply, a reply that drops part of the question — leaves the original text
untouched.

---

## Also worth trying

These behaved as specified in the same run, and are listed here rather than
expanded because they would repeat an entry above:

| Question | State | Why it is interesting |
|---|---|---|
| What is the minimum lump-sum investment amount for the HDFC ELSS Tax Saver Fund? | answered | Cites the `About` section rather than the key-stat block. |
| Where can I find the SID / KIM for the HDFC Equity Fund? | answered | A question phrased around a document still resolves to a factual answer. |
| How do I download my capital-gains statement? | no_answer | A question the brief names, whose answer these five scheme pages do not actually carry. See the known limits in `README.md`. |
| Is HDFC Large Cap safe for a 3-year goal? | refusal | Suitability phrasing, caught without the word "recommend". |
