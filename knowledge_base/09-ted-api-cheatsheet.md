# TED API cheatsheet (tested 2026-10-30)

Endpoint: `POST https://api.ted.europa.eu/v3/notices/search`
Headers: `Content-Type: application/json`. No API key for search.
Reference implementation: `../ted_open_tenders.py`.

```json
{"query": "<expert query>", "fields": ["publication-number", "PD", "TI"], "limit": 100, "page": 1}
```

## Tested queries (counts as of 2026-10-30) `[VERIFIED 2026-10-30]`
| Goal | Query | Result |
|---|---|---|
| Open tenders (repo default) | `PD>=today(-30) AND (DT>=today(0) OR DD>=today(0))` | used by script |
| Defence-directive notices, last 30 days | `legal-basis=32009L0081 AND PD>=today(-30)` | 530 |
| Defence award notices with winners, last year | `legal-basis=32009L0081 AND notice-type IN (can-standard can-modif) AND PD>=today(-365)` | 2,703 |
| CPV subtree (weapons & ammo), last 30 days | `classification-cpv=35300000 AND PD>=today(-30)` | 89 (children included) |
| Polish Armament Agency as buyer, last year | `buyer-name~"Agencja Uzbrojenia" AND PD>=today(-365)` | 42 |

## Fields confirmed to return data
`publication-number`, `ND`, `PD` (published), `TI` (title, dict by language), `DT` (tender
deadline), `DD` (participation deadline), `notice-type`, `buyer-name`, `buyer-country`,
`winner-name`, `winner-country`, `classification-cpv`, `result-value-notice`.

## Response shape
```json
{"notices": [{...}], "totalNoticeCount": 530, "iterationNextToken": "...", "timedOut": false}
```
- Most fields are **arrays** with repeats per lot → dedupe.
- `TI` and some names are dicts keyed by 3-letter language (`eng`, `pol`, …). Prefer `eng`,
  fall back to the first value.
- Notice HTML: `https://ted.europa.eu/en/notice/{publication-number}/html`.

## Pagination
- Small pulls: `"page": N`.
- Large pulls: add `"paginationMode": "ITERATION"`; the response returns
  `iterationNextToken` — pass it back to get the next batch. `[VERIFIED 2026-10-30]`
  (exact request field name for the token: check TED docs before building the loop).

## Notes
- Defence-directive notices still show `notice-type: cn-standard` / `can-standard`; use
  `legal-basis` to separate them, not the notice type.
- Example award: 716918-2025 — BAAINBw (DEU) → Rheinmetall MAN Military Vehicles GmbH, CPV 35400000.
- Example single-source: 419618-2026 — AU → WB Electronics (GLADIUS), negotiated without publication.
- Be polite: small `limit`, cache results, no tight loops (challenge ground rule).
