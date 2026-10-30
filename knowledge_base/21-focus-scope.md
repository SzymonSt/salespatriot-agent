# Focus scope — what we work on

`[TEAM DECISION]` **Geography: all of Europe** — every buyer and supplier country on TED, not just
Poland. For the demo we pull data **only from TED** and focus on **exactly four CPV
classes** under 35300000 "Weapons, ammunition and associated parts", **supplies only**.

## The four categories
| Category | CPV class | TED UI count | API count (`scope: ALL`) | API, latest versions only |
|---|---|---|---|---|
| Ammunition | `35330000` | 1490 | 1490 | 1273 |
| Firearms | `35320000` | 443 | 443 | 397 |
| Parts of firearms and ammunition | `35340000` | 264 | 264 | 224 |
| Miscellaneous weapons | `35310000` | 145 | 145 | 132 |

`[VERIFIED 2026-10-30]` API counts match the TED UI facets exactly. The four classes together
cover **2,230** distinct notices (some notices sit in several classes; the class counts add up to 2,342).

Origin: the TED search the team chose (also in `../CLAUDE.md`):
`notice-type = qu-sy, pin-cfc-standard, subco, cn-desg, cn-social, cn-standard, pin-cfc-social`,
`facet.cpv = dese, 35000000, 35300000`, `contract-nature = supplies`.
`dese` is a TED UI facet value with no known API equivalent.

## Queries
**Demand (notices you can still bid on):**
```
notice-type IN (qu-sy pin-cfc-standard subco cn-desg cn-social cn-standard pin-cfc-social)
AND contract-nature IN (supplies)
AND classification-cpv IN (35310000 35320000 35330000 35340000)
```
Add `AND (deadline-receipt-tender-date-lot>=today(0) OR deadline-receipt-request>=today(0))`
for **currently open** tenders: **38** on 2026-10-30.

**Supply (who won, for supplier profiles):**
```
notice-type IN (can-standard can-desg can-social can-tran)
AND contract-nature IN (supplies)
AND classification-cpv IN (35310000 35320000 35330000 35340000)
```
2,610 award notices in total, 325 in the last 365 days. The same award types are used in
`../ted_company_profiles.py`.

## Numbers as of 2026-10-30
| Slice | Notices |
|---|---|
| Competition notices, 4 classes | 2,230 |
| … under the defence directive (`legal-basis=32009L0081`) | 1,233 |
| … published in the last 365 days | 379 |
| … open now (deadline ≥ today) | 38 |
| Award notices, 4 classes | 2,610 |
| … last 365 days | 325 |
| Whole 353 subtree (for comparison) | 2,745 |
| 35 + 353 (the original UI search) | 37,248, mostly fire, police and security noise |

**Open now, by buyer country** (38): ESP 6, FRA 5, ROU 4, LTU 3, CZE 3, BGR 3, POL 2, EST 2,
plus SVN, SVK, HUN, LVA, DEU, BEL, AUT, CHE, IRL, DNK, 1 each. All are `cn-standard`.
Examples: 749708-2026 PL naval decoy ammunition (deadline 3 Dec) · 730745-2026 PL 7.62×39 mm
framework (2 Nov) · 719421-2026 ES 155 mm ammunition · 688238-2026 CZ special-forces pistols ·
637078-2026 AT 12 million 7.62 mm rounds.

## Noise to filter out
- **`35341100` "Gunmetal pipe fittings"** sits under "Parts of firearms" but is used for bronze
  plumbing parts: a Swiss water-pipe tender (658634-2026) and an Irish course-consumables
  dynamic purchasing system (448021-2026) appear in the open list. 23 competition notices
  carry it → exclude when it is the only focus code.
- Long-running dynamic purchasing systems (deadlines in 2031–2034) technically count as
  "open" (e.g. DK 403428-2025 drones, LT 733720-2024 clothing). Flag them separately.
- Notices where a focus CPV is one lot among many unrelated ones → rank by share of focus CPVs.
- The same procedure appears several times (corrigenda, re-publications: RO SIMMUNITION
  712456 / 721424 / 755822) → group by procedure, or use `onlyLatestVersions: true`.

## Where the data already lives in the repo
- `../ted_open_tenders.py`: export of any expert query to CSV.
- `../ted_company_profiles.py`: buyer and supplier profiles for this scope → `profiles/buyers.json`, `profiles/suppliers.json`.
- `../ted_supplier_contracts.py`: per-supplier won contracts (lot by lot) from notice XML.
