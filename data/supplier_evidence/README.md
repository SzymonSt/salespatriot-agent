# Supplier-provided evidence

Suppliers can add documents that free open data cannot show (licences, clearances,
certificates, accounts). One file per supplier, named after the profile id:
`data/supplier_evidence/<supplier id>.json` (e.g. `supplier-cze-65414128.json`).

After adding or changing a file, re-score from cached open data in a few seconds:

```bash
python3 supplier_due_diligence.py --offline --only supplier-cze-65414128
```

## Format

```json
{
  "supplier_id": "supplier-cze-65414128",
  "documents": [
    {
      "type": "iso_9001",
      "title": "ISO 9001:2015 certificate",
      "issuer": "Example Certification Body s.r.o.",
      "number": "CZ-12345",
      "valid_until": "2028-03-31",
      "file": "uploads/supplier-cze-65414128/iso9001.pdf",
      "uploaded_at": "2026-10-30",
      "verification_status": "declared"
    }
  ]
}
```

`verification_status`:
- `declared` (default) — uploaded by the supplier, not checked. Counts **60%** of the points.
- `document_verified` — a reviewer checked the document against the issuer (e.g. IAF CertSearch
  for ISO, NSPA NCAGE search, the national licensing authority). Counts **100%**.

Expired documents (`valid_until` in the past) are listed but add no points.

## Document types and points

| `type` | Category | Points (verified) |
|---|---|---|
| `arms_trade_licence` | Defence credentials | 2.5 |
| `export_licence` | Defence credentials | 2.0 |
| `facility_security_clearance` | Defence credentials | 2.0 |
| `aqap` | Defence credentials | 1.5 |
| `ncage` | Defence credentials | 1.5 |
| `certider` | Defence credentials | 1.5 |
| `nspa_source_file` | Defence credentials | 1.0 |
| `iso_9001` / `en_9100` | Defence credentials | 1.0 |
| `customer_reference` | Track record | 0.5 each |
| `financial_statements` | Financial health — used only when no open financial data exists. Add `year`, `currency`, `revenue`, `net_profit` | up to 6 |
| `ubo_declaration` | Ownership — used only when no public ownership data exists. Add `owners: [{name, country, share}]` | up to 6 |

Category scores are capped at 10, so documents raise a weak category but cannot push past it.
