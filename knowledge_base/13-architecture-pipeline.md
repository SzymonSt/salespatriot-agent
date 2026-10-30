# Architecture and data pipeline

`[HYPOTHESIS]` proposed architecture.

## End-to-end
```
01 DATA INGESTION        TED · e-Zamówienia · AU · NSPA · EDA · company registries · supplier sites
02 NORMALISATION         parsing · dedup · entity resolution · CPV mapping · translation · OCR
03 KNOWLEDGE GRAPH       companies · capabilities · buyers · awards · relationships (claims)
04 VECTOR EMBEDDINGS     descriptions · specs · semantic search
05 AGENTIC MATCHING      buyer agent ↔ supplier agent · scoring · negotiation
06 AUTONOMOUS DEAL       offer · negotiation · acceptance · transaction record + audit trail
```

## Collectors → canonical model → one API
- Each source has its **own collector** (adapter + update logic).
- Collectors convert to **one canonical data model** (ids, CPV, metadata, relationships).
- Everything goes behind **one Unified Supply Chain API**.
- RAG / vector search and the graph read from the same layer.
- **Agents talk only to the API**, never to source portals directly.
- The API standardises access; it does not duplicate business logic.

```
TED ─┐
AU  ─┼─> source collectors ─> canonical model ─> Unified API ─┬─> RAG / vectors
BZP ─┘                                                        ├─> ontology / graph
                                                              └─> agentic marketplace
```

## Scraping pipeline (for non-API sources such as AU / BIP)
SOURCE DISCOVERY → COLLECTOR (HTTP, API, HTML parser) → DOCUMENT DOWNLOADER
(PDF, DOCX, XLSX, ZIP, images) → PARSING & NORMALISATION (metadata, OCR) → SUPABASE.

## Supabase schema — ingestion side
| Table | Holds |
|---|---|
| `tenders` | Notice metadata |
| `tender_parts` | Lots |
| `documents` | Attachments: storage path, **file hash**, link to tender |
| `tender_updates` | Change history |
| `contractors` | Buyers and contractors |
| `tender_contacts` | Contact data (minimise personal data) |
| `tender_sources` | Source URLs |

Files go to Supabase Storage; the DB keeps metadata, path, hash and tender link.

## Supabase schema — marketplace side `[OWN KNOWLEDGE]` additions
| Table | Holds |
|---|---|
| `companies` | Canonical company + identifiers (`scheme:value`: EU VAT, LEI, NCAGE, national ids) |
| `capabilities` | Capability taxonomy, CPV mapping |
| `claims` | Evidence-backed graph edges (see [10](10-ontology-data-model.md)) |
| `credentials` | Licences and certificates with `valid_to` |
| `checks` | KYC (K1–K11) and per-deal (D1–D14) check results, HARD/SCORE, evidence ([22](22-kyc-and-deal-due-diligence.md)) |
| `demands` | Buyer needs (spec, qty, deadline, budget, limits) |
| `offers` | Supplier quotes |
| `deals` | Final agreed terms, status |
| `agent_messages` | Every agent-to-agent message |
| `audit_log` | Every autonomous action, with the rule that allowed it |

## Legal and safety
- Only publicly published resources. Respect site terms and rate limits; cache.
- Classified-related documents (e.g. AU notices mentioning **"ZASTRZEŻONE"**) are not
  ordinary attachments: do not try to obtain them.
- No ITAR/EAR, classified, CUI or customer data (challenge rule).

## First proof of concept (per new source)
- Pull 10–20 public procedures; extract base metadata.
- Download all public attachments; detect formats.
- Check whether PDFs have a text layer or need OCR.
- Find how updates are detected.
- Measure time to fetch and process one procedure.
- For HTML portals: Chrome DevTools → Network → Fetch/XHR (JSON?) and Doc (page URLs) before
  writing a scraper.
