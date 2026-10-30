# Company data sources (supply side) — agentless enrichment, Europe-wide

`[TEAM DECISION]` Scope is **all of Europe**, not Poland. KYC uses an **EU-wide baseline**
that works for any company, plus **country adapters** for national registers. Basic company
research runs **agentless**: plain code calls the APIs, and agents only interpret the results.

## EU-wide baseline (works for any EU company)
| Source | What it gives | Access | Status |
|---|---|---|---|
| **VIES** (EU VAT check) | VAT number valid; name and address where the member state shares them (Germany returns `---`) | `GET https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{CC}/vat/{number}`, no key | `[VERIFIED 2026-10-30]` DE and IT tested |
| **GLEIF** | LEI, legal name, address, status, **parent / ultimate parent** → foreign-control check | `https://api.gleif.org/api/v1/lei-records?...`, no key. An exact-name filter can hit a sister company → match on the LEI or on the address | `[VERIFIED 2026-10-30]` |
| **TED award history** | Contracts won, buyers, CPVs, values, `winner-owner-nationality`, `winner-size` | TED Search API by `winner-identifier` / `winner-name` | `[VERIFIED 2026-10-30]` |
| **Sanctions** — EU consolidated list (FSF), OpenSanctions | Sanctions and PEP screening | EU list: free download. OpenSanctions API needs a key (401 without one); its bulk data is free for non-commercial use | API 401 `[VERIFIED]`; rest `[OWN KNOWLEDGE]` |
| **NCAGE** (NSPA) | Company codified as a NATO supplier | Public lookup tool | `[OWN KNOWLEDGE — check URL]` |
| **CERTIDER** (EU) | Companies certified under Directive 2009/43/EC (intra-EU defence transfers) | Public web register | `[OWN KNOWLEDGE]` |
| IAF CertSearch / IAQG OASIS | Validity of ISO 9001 / EN 9100 certificates | Web | `[OWN KNOWLEDGE]` |
| BRIS / e-Justice business register search, EU insolvency register interconnection | Cross-border register and insolvency lookups | Web portals, no simple public API | `[OWN KNOWLEDGE]` |

## Country adapters (national registers)
Each adapter maps a national register into the same fields: `exists`, `active`, `legal_form`,
`address`, `representatives`, `activity_code`, `insolvency`. Build adapters in the order buyers
and suppliers appear in our scope ([21](21-focus-scope.md)).

| Country | Source | Notes | Status |
|---|---|---|---|
| PL | **KRS API** `api-krs.ms.gov.pl/api/krs/OdpisAktualny/{KRS}?rejestr=P&format=json` | Representatives, PKD activity codes, arrears, bankruptcy. No key | `[VERIFIED 2026-10-30]` |
| PL | **VAT white list** `wl-api.mf.gov.pl/api/search/nip/{NIP}?date=…` | NIP → REGON, KRS, VAT status, bank accounts. Daily limits | `[VERIFIED 2026-10-30]` |
| CZ | **ARES** `ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{IČO}` | Name, seat, legal form. No key | `[VERIFIED 2026-10-30]` |
| FR | **Recherche d'entreprises** `recherche-entreprises.api.gouv.fr/search?q=…` | SIREN, NAF activity code (KNDS France → 25.40Z, weapons and ammunition manufacturing), headcount band. No key | `[VERIFIED 2026-10-30]` |
| RO | ANAF VAT-payer web service | Free | `[OWN KNOWLEDGE — verify]` |
| DE | Handelsregister / Unternehmensregister | No simple free API → VIES + GLEIF + TED | `[OWN KNOWLEDGE]` |
| ES, IT, others | National registers (often paid) | Fall back to VIES + GLEIF + TED | `[OWN KNOWLEDGE]` |

Activity codes line up across countries: PL PKD, FR NAF and other national codes all derive
from **NACE** (25.40 = manufacture of weapons and ammunition). → Store the NACE code as the
common capability signal.

## How each source maps to evidence
| Check | Source | Evidence level |
|---|---|---|
| Exists, active, not insolvent | Country adapter; VIES | Document-verified |
| Who can sign | Country adapter (where it lists representatives) | Document-verified |
| What it does | NACE code from the register, TED CPVs, website | Code = document-verified; website = declared |
| Financial standing | Filed accounts (where available), size band | Document-verified |
| Past performance | TED awards | Document-verified |
| Foreign ownership | GLEIF parents, TED `winner-owner-nationality` | Document-verified |
| Sanctions | Sanctions lists | Document-verified (a screening, not proof of innocence) |
| Licences, certificates | Upload + CERTIDER / IAF / OASIS lookups | Declared → document-verified when matched |
| Free capacity | Supplier agent input | Declared (with date) |

## Rules
- Cache every response with `source_url` and `observed_at`.
- Respect rate limits.
- Registers list board members by name, which is personal data → store only what the feature needs.
- Beneficial-owner registers: public access was curtailed after the CJEU judgment of
  22 Nov 2022 (C-37/20, C-601/20), and many states now require a legitimate interest.
  → Use GLEIF parents plus the supplier's own declaration. `[OWN KNOWLEDGE]`
- No customer data, nothing classified (challenge rule).
