# Ontology and data model

`[HYPOTHESIS]` design proposal. Keep it small and correct; don't spend half the day on it.

## Why an ontology at all
A supplier writes "precision CNC machining", "aluminium parts", "metalwork for aerospace".
A tender asks for "aluminium machined structural components with tolerance X".
Keyword search misses this. We need:
- **Ontology / graph** — what kinds of things exist and how they relate.
- **Embeddings** — find similar text even with different words.

Embeddings are **not** an ontology: similar text does not prove a firm has the capability.

## Two parts
**A. Base ontology — fixed by us.** Entities, relations and categories defined up front.
LLM extraction must map into this schema and never invent new classes on its own.

**B. Pattern library — grows from data.** Recognised patterns (see
[12-historical-intelligence.md](12-historical-intelligence.md)). New *observations* can be
added automatically; changes to the *schema* go through validation (human-approved proposal queue).

Reuse the EU **eProcurement Ontology (ePO)** for procurement entities (buyer, notice, lot,
award, contract) and extend it with capabilities, facilities and business relationships.

## Entities
| Entity | Key attributes |
|---|---|
| Company | id, name, country, size, address, identifiers (KRS, NIP, REGON, VAT, LEI, NCAGE) |
| Buyer | organisation, type (MoD, agency, prime), budget signals, categories bought |
| Supplier | organisation (Company role), offered capabilities |
| Capability | process, technology, product or service (own taxonomy, mapped to CPV) |
| Certification | type (ISO 9001, EN 9100, AQAP…), issuer, valid from/to, scope |
| Tender | id, source, CPV, description, value, deadline, lots |
| Contract / Award | value, date, buyer, winner(s), subcontractors if disclosed |
| Requirement | technical, quantity, quality, certification, delivery parameters |
| Facility | location, processes, machines, capacity |
| Material | type, origin, availability |
| Deal | buyer, supplier(s), offer, price, delivery, status, agent transcript |
| Relationship | supplier-of, subcontractor-of, partner, consortium member |

## Example relations
```
Company A
  ├── HAS_CAPABILITY → CNC machining
  ├── HAS_CERTIFICATION → ISO 9001
  ├── LOCATED_IN → Poland
  ├── WON → Contract 123
  └── SUPPLIED_TO → Company B

Contract 123
  ├── PUBLISHED_BY → Buyer X
  ├── HAS_CPV → 35400000
  └── HAS_VALUE → €500,000
```

## Every claim carries evidence
Store relationships as **claim records**, not bare edges:

| Field | Meaning |
|---|---|
| `subject`, `predicate`, `object` | The claim |
| `source_id`, `source_url` | Where it came from |
| `observed_at` | When we saw it |
| `valid_from`, `valid_to` | Validity window if known (certificates expire!) |
| `confidence` | 0–1 |
| `verification_status` | `declared` · `document_verified` · `independently_verified` · `peer_attested` · `inferred` |

```json
{
  "subject": "company:krs:0000123456",
  "predicate": "HAS_CAPABILITY",
  "object": "capability:cnc_5axis_machining",
  "source_url": "https://example-supplier.pl/oferta",
  "observed_at": "2026-10-30T10:00:00Z",
  "confidence": 0.85,
  "verification_status": "declared"
}
```

Keep apart: **fact** (registry, award notice), **company claim** (website, profile),
**model inference** (LLM guessed it), **peer attestation** (another agent's experience, see
[15-agent-due-diligence-network.md](15-agent-due-diligence-network.md)).

## Identifiers and entity resolution
- One canonical company id; map all source names to it ("WB ELECTRONICS S.A.",
  "WB Electronics SA" → same company).
- Prefer hard ids: KRS/NIP/REGON (PL), VAT (EU), LEI, NCAGE. Fall back to normalised name +
  country + city, and mark the match `inferred`.

## Hackathon-sized model (first build)
Company · Capability · Certification · Tender · Award · Demand · Offer · Deal · Claim.
Everything else later.
