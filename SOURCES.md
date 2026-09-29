# Sources

The complete list of pages this assistant may read and cite. Generated
from the source registry in `src/sources.py` — the same registry the
ingestion pipeline fetches from and the query path validates every
citation against. No page outside this list is ever ingested or
cited (SC-1, SC-9, PRD §9.8).

Registry size: 5. Chunks ingested: 144.

## AMC

HDFC Mutual Fund (HDFC AMC), via the public fund-distributor
pages below.

## The five ingested pages

| # | Scheme (as the page titles it) | Brief label | URL | Fetched |
|---|---|---|---|---|
| 1 | HDFC Large Cap Fund Direct Growth | HDFC Large Cap Fund — Direct Growth (Large Cap) | <https://groww.in/mutual-funds/hdfc-large-cap-fund-direct-growth> | 2026-09-29T07:35:12+00:00 |
| 2 | HDFC Flexi Cap Direct Plan Growth | HDFC Equity Fund — Direct Growth (Flexi Cap) | <https://groww.in/mutual-funds/hdfc-equity-fund-direct-growth> | 2026-09-29T07:35:12+00:00 |
| 3 | HDFC ELSS Tax Saver Fund Direct Plan Growth | HDFC ELSS Tax Saver Fund — Direct Plan Growth (ELSS) | <https://groww.in/mutual-funds/hdfc-elss-tax-saver-fund-direct-plan-growth> | 2026-09-29T07:35:12+00:00 |
| 4 | HDFC Small Cap Fund Direct Growth | HDFC Small Cap Fund — Direct Growth (Small Cap) | <https://groww.in/mutual-funds/hdfc-small-cap-fund-direct-growth> | 2026-09-29T07:35:12+00:00 |
| 5 | HDFC Balanced Advantage Fund Direct Growth | HDFC Balanced Advantage Fund — Direct Growth (Balanced Advantage) | <https://groww.in/mutual-funds/hdfc-balanced-advantage-fund-direct-growth> | 2026-09-29T07:35:12+00:00 |

## Educational links shown with refusals

A refusal carries a fixed educational link instead of a source citation:
the link is not evidence for a fact, so it never gets a `Last updated`
stamp. These are curated constants in `config.EDUCATIONAL_LINKS`,
never model-written, and every host is on an allowlist.

| Purpose | Link |
|---|---|
| Official factsheets | <https://www.hdfcmf.com/> |
| Scheme details and risk disclosures | <https://www.hdfcmf.com/> |
| Regulator material on risk | <https://www.sebi.gov.in/> |
| Investor education | <https://www.amfiindia.com/> |
| The corpus source pages | <https://groww.in/> |

## Not ingested, deliberately

- Holdings tables, return calculators, historic-returns tickers,
  returns-and-rankings tables, and similar-fund comparison blocks.
  Performance and rankings are out of scope (PRD §4), and a
  stale returns table in the corpus is a number the app could quote.
- The live NAV. It changes daily and would contradict the
  `Last updated from sources:` stamp within a day.
- Distributor star ratings and category ranks (PRD §4).
- Fund-manager card lists of other HDFC schemes, which name
  schemes that are not in this corpus and invite citing one.
- AMC and RTA contact details.
- Third-party blogs, news sites, forums, and social media
  (PRD §9.2), and any back-end screenshots (PRD §9.3).

## Naming note

`HDFC Equity Fund` in the brief and the PRD is titled `HDFC Flexi Cap
 Direct Plan Growth` on the page served at that URL; the scheme was
renamed. Both names are recorded in the registry so the
discrepancy stays visible, and a question using the brief's name is
rewritten to the corpus name at query time (`sources.SCHEME_ALIASES`).

## Regenerating this file

```
.venv\Scripts\python.exe -m src.deliverables
```
