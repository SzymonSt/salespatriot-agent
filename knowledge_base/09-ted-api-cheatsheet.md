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
`winner-name`, `winner-country`, `winner-identifier` (free-text NIP/REGON/VAT, e.g.
`"NIP: 6630002096; Regon: 290539889"`), `organisation-identifier-tenderer`,
`classification-cpv`, `result-value-notice`.

For graph queries in ePO (SPARQL), see [19-ted-epo-ontology.md](19-ted-epo-ontology.md).

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
- Large pulls (>15,000 notices): add `"paginationMode": "ITERATION"`; the response returns
  `iterationNextToken` — send it back as `"iterationNextToken"` in the next request. Token
  valid until next OJ S release + 24 h. Full limits: [20-ted-api-reference.md](20-ted-api-reference.md).

## Notes
- Defence-directive notices still show `notice-type: cn-standard` / `can-standard`; use
  `legal-basis` to separate them, not the notice type.
- Example award: 716918-2025 — BAAINBw (DEU) → Rheinmetall MAN Military Vehicles GmbH, CPV 35400000.
- Example single-source: 419618-2026 — AU → WB Electronics (GLADIUS), negotiated without publication.
- Be polite: small `limit`, cache results, no tight loops (challenge ground rule).
