# salespatriot-agent

Tools for finding and profiling EU public tenders on TED (Tenders Electronic Daily).

## Focus: target search

We focus on the offers returned by this TED search:

https://ted.europa.eu/en/search/result?notice-type=qu-sy%2Cpin-cfc-standard%2Csubco%2Ccn-desg%2Ccn-social%2Ccn-standard%2Cpin-cfc-social&search-scope=ALL&scope=ALL&onlyLatestVersions=false&facet.cpv=dese%2C35000000%2C35300000&facet.contract-nature=supplies&sortColumn=publication-number&sortOrder=DESC&page=1

What the filters mean:

| URL param | Value | Meaning |
|---|---|---|
| `notice-type` | `cn-standard`, `cn-social`, `cn-desg`, `pin-cfc-standard`, `pin-cfc-social`, `qu-sy`, `subco` | Contract notices (standard, social, defence directive), PINs used as call for competition, qualification systems, subcontracting notices — i.e. notices you can still bid on, no award/result notices |
| `facet.cpv` | `35000000`, `35300000`, `dese` | CPV 35 = security, fire-fighting, police and defence equipment; 353 = weapons, ammunition and associated parts. `dese` is a TED UI facet value with no known API equivalent |
| `facet.contract-nature` | `supplies` | Supplies only (no works/services) |
| `onlyLatestVersions` | `false` | Include superseded versions (corrigenda etc.) |
| sort | `publication-number DESC` | Newest first |

The search page is a JS app — don't scrape the HTML. Use the TED Search API instead
(`https://api.ted.europa.eu/v3/notices/search`, POST, no key needed), as `ted_open_tenders.py` does.
Expert query equivalent of the page:

```
notice-type IN (qu-sy pin-cfc-standard subco cn-desg cn-social cn-standard pin-cfc-social)
AND classification-cpv IN (35000000 35300000)
AND contract-nature IN (supplies)
SORT BY publication-number DESC
```

```bash
python3 ted_open_tenders.py --query "<expert query above>" --limit 100
```

`classification-cpv IN (35000000)` matches child codes too (e.g. 35110000, 35331500). It also matches
notices where CPV 35 is only one of many lots, so expect some noise (e.g. a playground tender with 35261000).
On 2026-10-30 the query returned 37,248 notices; narrowing to `35300000` (weapons/ammunition) gave 2,745.

Useful API fields: `publication-number`, `notice-type`, `publication-date`, `notice-title` (dict per language,
prefer `eng`), `buyer-name` (dict per language → list), `buyer-country`, `classification-cpv`,
`deadline-receipt-tender-date-lot`, `estimated-value-lot`, `estimated-value-cur-lot`, `links`.
Values on multi-lot notices are lists and often repeat — dedupe them.
Notice page: `https://ted.europa.eu/en/notice/-/detail/<publication-number>`.

## Scrape examples (fetched 2026-10-30)

Records shortened: titles in English, lists deduped.

```json
{"publication-number": "755822-2026", "notice-type": "cn-standard", "publication-date": "2026-10-30+01:00",
 "title": "Romania – Cartridges – Acord cadru furnizare muniții de antrenament SIMMUNITION",
 "buyer": "Unitatea Militara 02655", "country": ["ROU"], "cpv": ["35331500"],
 "deadline": [], "value": ["9662400", "8300000"], "cur": ["RON"]}

{"publication-number": "752151-2026", "notice-type": "cn-standard", "publication-date": "2026-10-29+01:00",
 "title": "Spain – Machine guns – Adquisición de Repuestos y Herramientas AMP 12,70 mm.",
 "buyer": "Jefatura de Asuntos Económicos del Mando de Apoyo Logístico", "country": ["ESP"], "cpv": ["35321300"],
 "deadline": [], "value": null, "cur": []}

{"publication-number": "750233-2026", "notice-type": "cn-standard", "publication-date": "2026-10-29+01:00",
 "title": "Bulgaria – Cartridges – „Доставка на боеприпаси за нуждите на Български стрелкови съюз ...“",
 "buyer": "БЪЛГАРСКИ СТРЕЛКОВИ СЪЮЗ Сдружение", "country": ["BGR"], "cpv": ["35331500"],
 "deadline": ["2026-11-27+02:00"], "value": ["325000.00"], "cur": ["EUR"]}

{"publication-number": "755823-2026", "notice-type": "cn-standard", "publication-date": "2026-10-30+01:00",
 "title": "Croatia – Surveillance and security systems and devices – Dobava i ugradnja sustava tehničke zaštite",
 "buyer": "HEP d.d.", "country": ["HRV"], "cpv": ["35120000"],
 "deadline": ["2026-11-05+01:00"], "value": ["129000", "62000", "120000", "645000"], "cur": ["EUR"]}

{"publication-number": "755970-2026", "notice-type": "cn-standard", "publication-date": "2026-10-30+01:00",
 "title": "Germany – Firefighting vehicles – Lieferung Rüstwagen RW",
 "buyer": "Stadt Gersfeld (Rhön)", "country": ["DEU"], "cpv": ["34144210", "35111000"],
 "deadline": ["2026-11-27+01:00"], "value": null, "cur": []}

{"publication-number": "259064-2026", "notice-type": "cn-desg", "publication-date": "2026-04-16+02:00",
 "title": "Austria – Security, fire-fighting, police and defence equipment – Lieferung technischer ÖBB-Feuerwehrfahrzeuge HLF3/RLF Tunnel",
 "buyer": "ÖBB Infrastruktur AG", "country": ["AUT"], "cpv": ["35000000"],
 "deadline": [], "value": null, "cur": []}
```

An empty `deadline` usually means the notice uses a request-to-participate deadline instead — fetch
`deadline-receipt-request` too (e.g. 752151-2026 → 2026-12-16, negotiated with call; 259064-2026 → 2026-05-05).

## Repo layout

- `ted_open_tenders.py` — exports TED notices for an expert query to CSV (`open_tenders.csv`)
- `ted_company_profiles.py` — builds company/buyer profiles into `knowledge_base/profiles`
- `cpv_codes.py`, `knowledge_base/cpv_codes.csv`, `knowledge_base/cpv_defence_codes.csv` — CPV reference data
