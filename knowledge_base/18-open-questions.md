# Open questions and known gaps

Things we searched for and **did not find**, or still need to check. Agents: if a user asks
about these, say the data is missing instead of guessing.

## No data found
- How many hours or days European governments spend on due diligence / capability
  verification per supplier.
- A representative statistic for technical verification time in European MoDs.
- Public statistics on the number of factory audits of defence suppliers (UK or EU).
- Agencja Uzbrojenia's detailed supplier due-diligence process (documents per stage, plant
  visits, capacity measurement, staff hours, average duration).
- Any public, complete system that automatically verifies real production capacity of
  European firms.
- Current free production capacity of suppliers — not in any public dataset; must come from
  suppliers.

## Needs verification before quoting
- GLOBSEC May 2026 report: exact title, "280 firms / 15 interviews".
- Norway "4–5 years from project start to contract".
- EPRS 2025, IISS 2024, EU Payment Observatory, RUSI June 2026 figures.
- ~40% finance-access figure: does the 2026 EP SEDE study report it, or only the 2024 DG DEFIS study?
- UK DSPCR Chapter 13 wording and restricted-procedure periods (historic documents).

## Technical to-dos
- e-Zamówienia / BZP API: exact endpoints, parameters, rate limits (regulation PDF exists:
  `media.ezamowienia.gov.pl/pod/2023/01/Regulamin-korzystania-z-API.pdf`).
- AU BIP / purchasing platform: URL, JSON vs HTML, pagination, update detection.
- TED iteration pagination: exact request field for passing `iterationNextToken`.
- ~~TED RDF / SPARQL endpoint for notices in ePO~~ → resolved: `https://publications.europa.eu/webapi/rdf/sparql`, see [19](19-ted-epo-ontology.md).
- NCAGE public lookup tool URL and terms.
- VAT white-list daily limits.
- EDF results: format of participant lists (for the consortium graph).

## Product questions
- Which non-controlled item/service do we use for the autonomous demo deal?
- Which capability taxonomy do we start with (own list vs. mapped from CPV)?
- What does a supplier agent share in reference checks by default?
