# TED API v3 — reference (from the official docs and OpenAPI spec)

Sources: https://docs.ted.europa.eu/api/latest/index.html and the OpenAPI spec at
`https://api.ted.europa.eu/api-v3.yaml` (Swagger UI: `https://api.ted.europa.eu/swagger-ui/index.html`).
`[VERIFIED 2026-10-30]` unless marked. Tested queries: [09-ted-api-cheatsheet.md](09-ted-api-cheatsheet.md).
Our focus scope: [21-focus-scope.md](21-focus-scope.md).

## Which APIs exist
| API | Purpose | Key needed? |
|---|---|---|
| **Search** `POST /v3/notices/search` | Search published notices (expert query) | **No**. Anonymous, open to anyone |
| Visualisation `/v3/notices/render` | Render a notice as PDF/HTML | Published notices: no |
| Publication `/v3/notices/submit` … | eSenders submit notices | Yes (API key, 1 per EU Login) |
| Validation `/v3/notices/validate` | Validate notice XML | — |
| Conversion `/v3/notices/convert` | Old TED XML → eForms draft | — |

We only need **Search**, plus the notice XML/HTML links it returns.
Gateway pattern: `https://api.ted.europa.eu/{version}/{resource}/{action}`. The Search API is
not available in the Preview environment.

## Search request body
| Param | Default | Notes |
|---|---|---|
| `query` | required | Expert query, same syntax as https://ted.europa.eu/en/search/expert-search |
| `fields` | required | Fields to return (~1,800 allowed, listed in the OpenAPI enum) |
| `limit` | 10 | Notices per page, **max 250**. The spec says min 0, but the API rejects 0 (`SEARCH_BELOW_MIN_LIMIT`), so use 1 for count-only calls |
| `page` | 1 | PAGE_NUMBER mode only |
| `scope` | `ALL` | `ALL` all notices · `ACTIVE` only active notices · `LATEST` only the current OJ S release |
| `onlyLatestVersions` | `false` | `true` drops superseded versions (e.g. replaced by a corrigendum) |
| `paginationMode` | `PAGE_NUMBER` | or `ITERATION` |
| `iterationNextToken` | — | Token from the previous response; omit on the first ITERATION call |
| `checkQuerySyntax` | `false` | `true` only checks the syntax and returns a precise error location (tested) |

## Limits
| Mode | Max notices per page | Max fields per page (= `limit` × number of fields) | Max retrievable notices |
|---|---|---|---|
| PAGE_NUMBER | 250 | 10,000 | **15,000** per query, then errors |
| ITERATION (scroll) | 250 | 10,000 | Unlimited |

- ITERATION uses an Elasticsearch point-in-time. Pages are consistent (no duplicates or
  misses). The token expires at the **next OJ S release + 24 h**.
- PAGE_NUMBER is stateless: if an OJ S release lands between two pages, you can miss notices
  or get duplicates.
- No rate limit is documented. The challenge rule still applies: don't hammer the API, sleep
  between calls, cache. The team's scripts use a 1 s interval and 5 retries.
- Field-list changes are not versioned: fields can be added or removed without a version bump.

## Publication timing
- Notices are published in OJ S on working days. Export happens around 15:00 CET on the
  publication date, and the notice is live on TED no later than 09:00 CET on the next OJ S day.
- Implication: a daily sync after ~09:30 CET is enough. `scope: LATEST` returns that day's release only.

## Expert query cheatsheet
- Comparison: `=`, `!=`, `>=`, `<=`, `IN (a b c)` (space-separated), `NOT`, `AND`, `OR`, parentheses.
- Text match: `buyer-name~"Agencja Uzbrojenia"`.
- Dates: `PD>=today(-30)`, `deadline-receipt-tender-date-lot>=today(0)`.
- Sort: append `SORT BY publication-number DESC`.
- `classification-cpv IN (35300000)` includes all child codes.
- Debug any query with `"checkQuerySyntax": true` or in the Expert Search page (it lists all fields).

## Fields worth knowing (beyond the basics in 09)
| Need | Fields |
|---|---|
| Who won | `winner-name`, `winner-identifier`, `winner-country`, `winner-city`, `winner-size`, `winner-listed`, **`winner-owner-nationality`** (foreign-control check), `winner-decision-date`, `winner-selection-status` |
| Bidders | `organisation-identifier-tenderer`, `tender-value-lowest`, `tender-value-highest` |
| Subcontracting (Tier 2 links) | `subcontracting`, `subcontracting-value`, `subcontracting-percentage`, `subcontracting-description`, `organisation-identifier-subcontractor`, `subcontracting-obligation-lot` |
| Single source / direct award | `procedure-type`, `direct-award-justification-proc`, `direct-award-justification-text-proc` |
| Buyer | `buyer-name`, `buyer-identifier`, `buyer-country`, `buyer-legal-type`, `main-activity` |
| Scope and value | `classification-cpv`, `contract-nature`, `estimated-value-proc`/`-lot`, `framework-*`, `result-value-notice` |
| Deadlines | `deadline-receipt-tender-date-lot`, `deadline-receipt-request` (an empty tender deadline usually means a request-to-participate deadline) |
| Text | `notice-title`, `title-proc`, `description-proc`, `description-lot` |
| Legal | `legal-basis` (`32009L0081` = defence directive, `32014L0024` = classic directive) |
| Links | `links` → XML / PDF / HTML per language |

## Gotchas
- Multi-lot notices return lists with repeats → dedupe.
- Titles and names are dicts keyed by language (`eng`, `pol`…) → prefer `eng`.
- `winner-identifier` is free text in national formats (`NIP: …; Regon: …`, `DE8141…`, `HRB …`) → normalise it.
- Publication numbers: Search API `800547-2025`; SPARQL `00800547-2025`.
- Python from python.org on macOS may fail with `CERTIFICATE_VERIFY_FAILED` in `urllib`.
  Fix: run `Install Certificates.command` from the Python folder, or use `curl`. `[VERIFIED on one team laptop]`
