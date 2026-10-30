# OP-TED framework and the eProcurement Ontology (ePO) — decision

GitHub org: https://github.com/OP-TED (EU Publications Office, the TED team).
Checked on 2026-10-30, with live queries. `[VERIFIED 2026-10-30]` unless marked.

## Decision `[TEAM DECISION — proposed]`
**Use ePO for the procurement half of our model. Add a small extension of our own for the
marketplace half. Don't design our own procurement ontology, and don't run a triple store.**

## What is in OP-TED that matters to us
| Repo | What it is | How we use it |
|---|---|---|
| `ePO` | The eProcurement Ontology (OWL + SHACL). Latest release v5.2.0 (Oct 2025); TED Open Data is modelled in ePO 4.x | Vocabulary for buyers, procedures, lots, tenders, winners, contracts |
| `epo-knowledge` | ePO as LLM-friendly Markdown, one file per term (v4.2.0: 254 classes, 357 object properties, 200 datatype properties) | RAG for our agents (class definitions + properties) |
| `ted-open-data-examples` | 41 curated SPARQL queries (winners, buyers, CPV, amounts) + `llm-knowledge.yaml` format | Few-shot examples for agents writing SPARQL. **None targets defence** |
| `ted-open-data` | data.ted.europa.eu explorer: notice browser + SPARQL editor | Manual exploration, debugging |
| `ted-rdf-mapping-eforms` | RML rules eForms XML → ePO RDF | Not needed: TED already publishes the RDF |
| `entity-resolution-*` | OP's Entity Resolution Service (ERS/ERE contract, curation app), in development 2026 | Same problem we must solve; our resolver could follow their ERS–ERE contract |
| `ESPD-EDM` | Data model of the European Single Procurement Document (exclusion + selection criteria) | Codes for qualification requirements in supplier profiles |
| `eForms-SDK` | eForms fields (BT codes) and code lists | Field meanings when parsing notices |

## Two ways to get TED data — both tested
| | TED Search API (`api.ted.europa.eu/v3/notices/search`) | TED Open Data SPARQL (`publications.europa.eu/webapi/rdf/sparql`) |
|---|---|---|
| Format | JSON, flat fields | RDF in ePO, graph |
| Speed | Fast, paginated | Anchored on one notice ~1 s; one day of defence results ~8–10 s; wide multi-join queries time out (120 s); occasional `Transaction deadlock` → retry |
| Winners | `winner-name`, `winner-identifier`, `winner-country` | Full award chain, plus business size, beneficial owners, buyer main activity, procedure type, subcontractors |
| Use for | **Bulk ingestion**: open tenders and award history | **Enrichment and ad-hoc agent questions** |

`winner-identifier` example from the Search API: notice 800547-2025 → `"NIP: 6630002096; Regon: 290539889"`.

## What the live ePO data looks like
- Procedure: `epo:hasLegalBasis <…/legal-basis/32009L0081>` (defence directive),
  `epo:hasProcedureType <…/procurement-procedure-type/neg-wo-call>`.
- Buyer: `epo:hasMainActivity <…/main-activity/defence>`, `epo:hasBuyerLegalType <…/buyer-legal-type/cga>`.
- Organisation: `epo:hasLegalName`, `epo:hasLegalIdentifier/skos:notation`,
  `epo:hasBusinessSize <…/economic-operator-size/large>`, `epo:hasBeneficialOwner`, `m8g:registeredAddress`.
- Organisation URIs are scoped to one notice (`…_Organization_ORG-0001`), so the same company
  has no shared id across notices. OP-TED lists this as a known limitation.
- Legal identifiers are free text in mixed formats: `NIP: 6630002096; Regon: 290539889`,
  `REGON: 190577628`, `DE814129219`, `HRB 139798`, `00721920155`, `A22363733`.
- Data errors happen: "steep GmbH" appears as both `DE814129219` and `DE81412919`; one
  contractor name is "keine Angabe" ("not specified").
- `epo:Tenderer` means every bidder. Winners need `LotAwardOutcome` with
  `hasAwardStatus …/winner-selection-status/selec-w` and `TenderAwardOutcome hasAwardRank 1`.
  On 2026-10-28 the strict winner filter returned 2 defence winners, while 17 tenderer rows
  appeared in the same result notices. Many notices don't fill rank/status → keep a
  fallback "tenderer in a result notice" with lower confidence.

## Proven chain: TED → national registry
TED 800547-2025 (buyer: Agencja Uzbrojenia) → winner MESKO S.A., `NIP 6630002096` →
VAT white list → `KRS 0000041811` → KRS API → main PKD **25.40.Z "Produkcja broni i
amunicji"** (manufacture of weapons and ammunition). A public award becomes a
document-verified supplier profile with no LLM involved.

## Why ePO (pros)
- It already models our demand side: `Buyer`, `Procedure`, `Lot`, `Tender`, `Tenderer`, `Winner`,
  `Contract`, `FrameworkAgreement`, `Subcontractor`, `Subcontract-D81` (defence directive),
  `OrganisationGroup` (consortia), `SelectionCriterion` (suitability, financial standing,
  technical/professional ability), `Certificate`, `CertificationLabel`, `Business`.
- TED already publishes in it → no mapping work for our main source.
- It is the official EU standard. That counts for "use of data" and makes us interoperable.
- It is documented in an LLM-ready form (`epo-knowledge`) and has example queries.

## What ePO lacks (we add it)
- **Supplier capabilities**: processes, machines, facilities, capacity, lead times.
  ePO records what was bought, not what a supplier can make.
- **Private B2B market**: demand from primes, offers, negotiation, deals between agents.
- **Provenance**: confidence, evidence level, peer attestations on every statement.
- **Cross-notice company identity**.
- Practicality: running a triple store and an OWL reasoner is too heavy for one day.

## Hybrid model
- Procurement entities use ePO class and property names. We keep the original URIs,
  publication numbers and EU code-list URIs as ids.
- Storage: Postgres (Supabase). Tables are named after ePO classes (`procedure`, `lot`,
  `organisation`, `award_outcome`…), columns after ePO properties (`legal_name`,
  `business_size`, `legal_basis`…). No triple store.
- Extension namespace `sp:` (SalesPatriot): `Company` (canonical), `Capability`, `Credential`,
  `Demand`, `Offer`, `Deal`, `Check`, `Claim`. See the compact graph in [10](10-ontology-data-model.md).
- `sp:Company` ←`owl:sameAs`→ each per-notice ePO organisation.
- EU authority tables (legal-basis, buyer-legal-type, main-activity, economic-operator-size,
  winner-selection-status) are used as enums. ESPD-EDM criterion codes describe qualification requirements.

## Implementation steps (hackathon-sized)
1. **Open tenders**: keep `ted_open_tenders.py` (Search API). Add a `legal-basis=32009L0081` option.
2. **Award history**: Search API with fields `winner-name, winner-identifier, winner-country,
   buyer-name, classification-cpv, result-value-notice` over the last 365 days of defence
   awards (2,703 notices). Use iteration pagination. Cache in Supabase.
3. **Identifier normaliser**: regex the free text into typed ids (`PL:NIP`, `PL:REGON`, `PL:KRS`,
   `DE:VAT`, `IT:VAT`/fiscal code…). Resolve Polish ids via VAT white list → KRS; EU VAT via
   VIES. Output: canonical `Company` + claims marked `document_verified`.
4. **Enrichment over SPARQL** (per notice, cached): business size, beneficial owners,
   procedure type, subcontractors.
5. **Capability layer**: build `sp:Capability` from the winner's CPVs, KRS PKD codes and supplier input.
6. **Historical-intelligence agent**: RAG over `epo-knowledge` class files plus
   `ted-open-data-examples`, so the agent can write SPARQL for ad-hoc questions.
   Guardrails: SELECT only, always LIMIT, anchor on a date or notice, timeout, retry.

## Ready-to-run SPARQL: defence award winners for one day
```sparql
PREFIX epo: <http://data.europa.eu/a4g/ontology#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX dc: <http://purl.org/dc/elements/1.1/>
SELECT ?publicationNumber ?cpv (SAMPLE(?winnerName) AS ?winner) (SAMPLE(?winnerId) AS ?winnerLegalId) ?amount ?currency
WHERE {
  GRAPH ?g {
    ?notice a epo:ResultNotice ;
            epo:hasPublicationDate "2026-10-28"^^xsd:date ;
            epo:hasNoticePublicationNumber ?publicationNumber ;
            epo:refersToProcedure ?procedure ;
            epo:announcesAwardDecision / epo:comprisesAwardOutcome ?lotOutcome .
    ?procedure epo:hasLegalBasis <http://publications.europa.eu/resource/authority/legal-basis/32009L0081> .
    OPTIONAL { ?procedure epo:hasPurpose / epo:hasMainClassification ?cpvUri . }
    ?lotOutcome epo:hasAwardStatus <http://publications.europa.eu/resource/authority/winner-selection-status/selec-w> ;
                epo:comprisesTenderAwardOutcome [ epo:hasAwardRank 1 ; epo:concernsTender ?tender ] .
    ?tender epo:isSubmitedBy / epo:playedBy ?org .
    ?org epo:hasLegalName ?winnerName .
    OPTIONAL { ?org epo:hasLegalIdentifier / skos:notation ?winnerId . }
    OPTIONAL { ?tender epo:hasFinancialOfferValue [ epo:hasAmountValue ?amount ; epo:hasCurrency ?curUri ] . }
  }
  OPTIONAL { ?cpvUri dc:identifier ?cpv . }
  OPTIONAL { ?curUri dc:identifier ?currency . }
}
GROUP BY ?publicationNumber ?tender ?cpv ?amount ?currency
LIMIT 50
```
Sample result: `00747642-2026, 35730000, CARL ZEISS S.P.A. CON SOCIO UNICO, 00721920155, 1099300 EUR`.

Run it with: `curl -G https://publications.europa.eu/webapi/rdf/sparql --data-urlencode "query@q.sparql" -H 'Accept: text/csv'`.
In SPARQL, publication numbers are zero-padded: `00800547-2025`, not `800547-2025`.

## What we can improve, and possibly contribute back
1. **Cross-notice entity resolution backed by national registries.** This is the exact
   limitation OP-TED documents. Our resolver could be packaged as an ERE behind their ERS contract.
2. **Identifier normalisation**: free text → typed `scheme:value`.
3. **Capability layer** linking ePO winners to what they can make (CPV ↔ PKD ↔ capability).
4. **Defence query pack** (legal basis 2009/81 + buyer main activity = defence) as a PR to
   `ted-open-data-examples`, which has no defence examples yet.
