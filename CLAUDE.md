# salespatriot-agent

Tools for finding and profiling EU public tenders on TED (Tenders Electronic Daily).

## Focus: target search

We focus on the offers returned by this TED search:

https://ted.europa.eu/en/search/result?notice-type=qu-sy%2Cpin-cfc-standard%2Csubco%2Ccn-desg%2Ccn-social%2Ccn-standard%2Cpin-cfc-social&search-scope=ALL&scope=ALL&onlyLatestVersions=false&facet.cpv=dese%2C35000000%2C35300000&facet.contract-nature=supplies&sortColumn=publication-number&sortOrder=DESC&page=1

What the filters mean:

| URL param | Value | Meaning |
|---|---|---|
| `notice-type` | `cn-standard`, `cn-social`, `cn-desg`, `pin-cfc-standard`, `pin-cfc-social`, `qu-sy`, `subco` | Contract notices (standard, social), design contests (`cn-desg`), PINs used as call for competition, qualification systems, subcontracting notices (`subco`) — i.e. notices you can still bid on, no award/result notices. Defence-directive notices are `cn-standard` with `legal-basis=32009L0081`, not a separate type |
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

### Geography: all of Europe

Buyers and suppliers from every European country on TED, not only Poland. Company checks (KYC) use EU-wide
sources (VIES, GLEIF, TED, sanctions) plus per-country register adapters (PL, CZ, FR tested) —
see `knowledge_base/07-company-data-sources.md` and `knowledge_base/22-kyc-and-deal-due-diligence.md`.

### Focus categories — exactly these four

Within that search we work on exactly these CPV classes (TED UI facet counts = API counts, checked 2026-10-30):

| Category | CPV | Notices |
|---|---|---|
| Ammunition | `35330000` | 1490 |
| Firearms | `35320000` | 443 |
| Parts of firearms and ammunition | `35340000` | 264 |
| Miscellaneous weapons | `35310000` | 145 |

```
notice-type IN (qu-sy pin-cfc-standard subco cn-desg cn-social cn-standard pin-cfc-social)
AND contract-nature IN (supplies)
AND classification-cpv IN (35310000 35320000 35330000 35340000)
```

2,230 distinct notices; 38 open on 2026-10-30 (add
`AND (deadline-receipt-tender-date-lot>=today(0) OR deadline-receipt-request>=today(0))`).
Supplier side: the same CPVs with `notice-type IN (can-standard can-desg can-social can-tran)` → 2,610 award notices.
Drop `35341100` (gunmetal *pipe* fittings — plumbing noise) when it is a notice's only focus code.
Details, noise rules and samples: `knowledge_base/21-focus-scope.md`. API limits (250/page, 15k max in
page mode, `ITERATION` mode for more): `knowledge_base/20-ted-api-reference.md`.

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
- `knowledge_base/README.md` — index of the team knowledge base (challenge rules, pain points, data sources,
  TED API, ePO ontology decision, agent design, demo plan). Each file is small; load only what the task needs.
  Respect the evidence labels (`VERIFIED`, `REPORTED`, `HYPOTHESIS`…) — don't present unverified numbers as fact.
  Due diligence design (KYC on onboarding vs per-deal checks): `knowledge_base/22-kyc-and-deal-due-diligence.md`.
  Self-learning RAG over deals (TED + platform + uploaded): `knowledge_base/23-self-learning-rag.md`.
- `ted_company_profiles.py` — builds company/buyer profiles into `data/profiles`
- `cpv_codes.py`, `data/cpv_codes.csv`, `data/cpv_defence_codes.csv` — CPV reference data
- `supplier_due_diligence.py` + `dd_sources.py` — 1–10 supplier due-diligence score from free open data,
  written to `due_diligence` in `suppliers.json` and to `data/profiles/due_diligence_summary.md`.
  Method is in the module docstring; supplier-uploaded documents go in `data/supplier_evidence/`
  (see its README). Source responses are cached in `.cache/dd/`; `--offline` re-scores from the cache.

## Due diligence sources (free, no key, checked 2026-10-30)

| Scope | Source | Gives |
|---|---|---|
| CZ | ARES REST (`ekonomicke-subjekty`, `-vr`) | status, NACE, licensed activities, shareholders, officers, insolvency flag |
| PL | VAT white list + KRS `OdpisAktualny` | status, PKD, shareholders, filed accounts, dzial6 liquidation/bankruptcy |
| FR | recherche-entreprises.api.gouv.fr | status, NAF, revenue/profit |
| RO | ANAF `PlatitorTvaRest/v9` + `bilant` | status/inactive flag, CAEN, ownership form, balance sheet |
| SK | RPO (statistics.sk), RPVS OData, registeruz.sk | status, owners, UBOs (public-sector partners), filed accounts |
| DK | cvrapi.dk | status, bankruptcy flag, industry, employees |
| EE | ariregister autocomplete | status (R/L/N/K) |
| LV | data.gov.lv UR CSVs (streamed, indexed) | register, members, UBOs, insolvency, suspensions, accounts |
| EU/all | VIES, GLEIF (+ parents), Wikidata (via LEI), EU Funding & Tenders search (EDF/ASAP projects), GDELT news | VAT validity, LEI, parent group, industry, EU defence projects, adverse headlines |
| Sanctions | EU FSF CSV, UN XML, OFAC SDN/alt/consolidated, UK FCDO CSV | name screening of company, owners, officers |

Not free / not covered: DE, NL, ES, IT, IE, SE, SI, BG, CY, PT registers (VIES + GLEIF only);
UK Companies House needs a free key; OpenSanctions needs a commercial licence; EDES and World Bank
debarment lists have no clean public API; GDELT rate-limits to 1 request / 5 s.
