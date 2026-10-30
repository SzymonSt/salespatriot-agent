# Procurement data sources (demand side)

Rule from the brief: real public data only; respect site terms; don't hammer servers.

## TED — Tenders Electronic Daily (EU) — START HERE
- Official EU journal for above-threshold public procurement, incl. defence and security.
- **Search API v3**: `POST https://api.ted.europa.eu/v3/notices/search`, no key needed for
  search. Used by `../ted_open_tenders.py`. `[VERIFIED 2026-10-30]`
- Filter defence-directive notices with `legal-basis=32009L0081` (Directive 2009/81/EC):
  530 notices published in the last 30 days, 2,703 award notices in the last 365 days.
  `[VERIFIED 2026-10-30]`
- Award notices expose `winner-name`, `winner-country`, `buyer-name`, `classification-cpv`,
  values → history of who won what. Tested queries: [09-ted-api-cheatsheet.md](09-ted-api-cheatsheet.md).
- **eForms** became mandatory on 25 Oct 2023. eForms content includes buyer, scope, CPV,
  lots, estimated value, selection criteria (technical ability, financial standing), award,
  winner, contract value, **subcontracting info**, recurrence. `[OWN KNOWLEDGE]` +
  `[REPORTED]` (TED eForms docs)
- Linked data: CPV is available via the EU Publications Office SPARQL endpoint (used by
  `../cpv_codes.py`). The same endpoint serves TED notices as RDF in the **eProcurement
  Ontology (ePO)** — reuse it instead of designing an ontology from zero.
  `[VERIFIED 2026-10-30]` — see [19-ted-epo-ontology.md](19-ted-epo-ontology.md).

## e-Zamówienia (Poland)
- National platform run by the Public Procurement Office (UZP). Hosts the **Biuletyn Zamówień
  Publicznych (BZP)** — national (below EU threshold) notices. Above-threshold Polish
  notices go to TED. `[OWN KNOWLEDGE]`
- Has an official API with terms of use ("Regulamin korzystania z API") and a separate BZP
  web service for downloading national notices. `[VERIFIED 2026-10-30]` (regulation PDF exists)
- Third-party scrapers report read access without registration, fields: BZP number,
  subject, CPV, publication date, deadline; contact personal data stripped; **contractor
  names in award notices not exported**. `[REPORTED]`
- Exact endpoints not confirmed → see [18-open-questions.md](18-open-questions.md).

## Agencja Uzbrojenia — AU (Polish Armament Agency)
- Above-threshold AU procedures appear on TED: `buyer-name~"Agencja Uzbrojenia"` returned
  42 notices in the last 365 days. `[VERIFIED 2026-10-30]` → **use TED as the AU feed**.
- Many AU purchases use defence exemptions (Pzp Art. 12–13 / Art. 346 TFEU) or negotiated
  procedures without publication, so TED shows only part of AU demand. A 2025 Sejm
  interpellation (nr 11849) asked about AU's use of these exemptions. `[VERIFIED 2026-10-30]`
- Some documents are offline-only or linked to classified ("ZASTRZEŻONE") information.
  **Collect only what is publicly published. Never try to access restricted documents.**
- AU's own BIP / platform address and API not confirmed. Before scraping, open the page in
  Chrome DevTools → Network → Fetch/XHR to see if lists come as JSON or only HTML.

## NATO NSPA
- NSPA eProcurement portal; opportunities, notifications of intent, RFPs.
- To receive solicitations a firm must be in the **NSPA Source File** (needs NCAGE code;
  firm must be from an eligible NATO/NSPO nation). `[VERIFIED 2026-10-30]`
- Some documents require a registered login → use only public listings.
- Useful as a **signal**: "registered NSPA supplier / has NCAGE" is a strong defence-readiness
  marker for a supplier profile.

## European Defence Agency (EDA)
- EDA publishes its own calls for tender (also on TED) on its website. Volume is small.
- Better use of EDA for us: context on capability priorities and cooperation projects.
  `[OWN KNOWLEDGE]`

## Extra sources worth adding `[OWN KNOWLEDGE]`
| Source | Why |
|---|---|
| EU Funding & Tenders portal — **European Defence Fund (EDF)** calls and results | EDF collaborative projects need consortia (at least 3 entities from 3 eligible countries). Results list participants → **real historical collaboration graph** for consortium matching |
| UK Find a Tender (OCDS API), Contracts Finder | Large defence buyer (MOD), structured data |
| France BOAMP open data | National notices incl. defence |
| Germany: BAAINBw notices via TED | Already in TED; example award winner: Rheinmetall MAN Military Vehicles (TED 716918-2025) `[VERIFIED 2026-10-30]` |
| EU SAFE / EDIP / ASAP programme pages | Demand signals for the next years (ammunition, air defence, etc.) |

## Demand signals beyond tenders
- Prior information notices (PIN) on TED — planned purchases.
- Annual procurement plans (*plan postępowań*) — Polish public bodies must publish them.
- Framework agreements and their call-offs → recurring demand.
- EU instruments (SAFE loans, EDIP, EDF work programmes) → future demand by category.
