# Data model (compact graph)

`[HYPOTHESIS]` Keep it small. Procurement entities follow ePO
([19-ted-epo-ontology.md](19-ted-epo-ontology.md)); everything else is our `sp:` extension.
Embeddings help find similar text, but they do not prove a capability; the graph records what is known.

## Core graph (9 entities)
```
Company ─HAS_CAPABILITY→ Capability ─MAPS_TO→ CPV
Company ─HOLDS→ Credential (licence | certificate, valid_to)
Company ─WON→ Award (ePO, from TED) ─BY→ Company(buyer)
Company(buyer) ─POSTS→ Demand ←ANSWERS─ Offer ←MAKES─ Company(supplier)
Offer ─BECOMES→ Deal ─HAS→ Check (KYC or deal due diligence)
Any edge or value ─BACKED_BY→ Claim (source, time, evidence level)
```

| Entity | Key fields |
|---|---|
| Company | canonical id; identifiers (KRS, NIP, REGON, VAT, LEI, NCAGE); country; size; role (buyer/supplier); `kyc_status`, `kyc_checked_at` |
| Capability | own taxonomy; CPV and PKD mapping |
| Credential | type (arms concession, ISO 9001, EN 9100…), issuer, `valid_from` / `valid_to`, scope |
| Award | TED publication number, buyer, winner, CPV, value, date |
| Demand | buyer, spec (JSON), quantity, deadline, budget, delivery place |
| Offer | supplier, price, lead time, terms |
| Deal | buyer, supplier(s), agreed terms, status, verdict (GO, GO WITH CONDITIONS, ESCALATE, NO-GO), agent transcript |
| Check | category (K1–K11, D1–D14 in [22](22-kyc-and-deal-due-diligence.md)), result, HARD/SCORE, evidence |
| Claim | subject, predicate, object, `source_url`, `observed_at`, `confidence`, `verification_status` |

Deferred until after the demo: facilities, materials, subcontracting chains as separate entities.
For now, keep them as JSON fields or claims.

## Claims and evidence levels
`verification_status`: `declared` · `document_verified` · `independently_verified` ·
`peer_attested` · `inferred`. Keep fact (registry, award), company claim, model inference and
peer attestation apart.

```json
{"subject": "company:pl-krs:0000041811", "predicate": "HOLDS",
 "object": "credential:arms-concession", "source_url": "upload://…",
 "observed_at": "2026-10-30T10:00:00Z", "confidence": 0.9,
 "verification_status": "document_verified"}
```

## Rules
- The LLM maps text into this schema and never invents new classes. Schema changes go
  through a human-approved proposal queue.
- One canonical company id. Resolve by hard ids (KRS, NIP, VAT, LEI) first; a match on
  name + country is marked `inferred`.
