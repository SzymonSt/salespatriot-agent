# Company data sources (supply side) — agentless enrichment

`[TEAM DECISION]` Basic company research (registry data, public results, ratings, history)
runs **agentless or agent-minimal**: plain code calling APIs, no LLM reasoning loop. Agents
only interpret the result. Cheaper, faster, deterministic, easy to demo.

## Polish registries
| Source | What it gives | Access | Status |
|---|---|---|---|
| **KRS API** (Ministry of Justice) | Full current extract: name, legal form, address, capital, board/representation, PKD activity codes, filed financial statements, arrears, liquidation/bankruptcy | `GET https://api-krs.ms.gov.pl/api/krs/OdpisAktualny/{KRS}?rejestr=P&format=json` — no key. Sections `dzial1`…`dzial6` | `[VERIFIED 2026-10-30]` |
| **VAT white list** (Ministry of Finance) | Name, NIP, REGON, KRS, VAT status (*Czynny*), address, registration date, bank accounts | `GET https://wl-api.mf.gov.pl/api/search/nip/{NIP}?date=YYYY-MM-DD` — no key, has daily limits | `[VERIFIED 2026-10-30]` |
| REGON BIR (GUS) | REGON data, PKD codes | Free key required | `[OWN KNOWLEDGE]` |
| CEIDG | Sole traders (not in KRS) | API token required | `[OWN KNOWLEDGE]` |
| Financial statements repository (eKRS RDF) | Filed annual accounts (often XML) → financial standing | Public web | `[OWN KNOWLEDGE]` |

KRS sections (as returned): `dzial1` entity data · `dzial2` representation · `dzial3`
activity (PKD) and filed statements · `dzial4` arrears/claims · `dzial5` curator ·
`dzial6` liquidation, bankruptcy, restructuring. `[OWN KNOWLEDGE]` for section meanings.

## EU / international
| Source | What it gives | Status |
|---|---|---|
| **GLEIF API** (`api.gleif.org`) | LEI, legal name, address, **parent/ultimate parent** → foreign control check | `[OWN KNOWLEDGE]` |
| **VIES** | EU VAT number validity | `[OWN KNOWLEDGE]` |
| **TED award history** | Past contracts won (`winner-name`) → past performance (Directive Art. 42) | `[VERIFIED 2026-10-30]` |
| EU consolidated sanctions list / **OpenSanctions** | Sanctions and PEP screening | `[OWN KNOWLEDGE]` |
| **NCAGE** public search (NSPA) | Firm is codified as a NATO supplier | `[OWN KNOWLEDGE — check tool URL]` |
| IAF CertSearch | Validity of accredited ISO certificates (e.g. ISO 9001) | `[OWN KNOWLEDGE]` |
| IAQG OASIS | EN/AS 9100 certified suppliers | `[OWN KNOWLEDGE]` |
| Company website, news, reviews (e.g. GoWork in PL) | History, products, reputation | Low confidence, label as such |

## How each source maps to evidence
| Check | Source | Evidence level |
|---|---|---|
| Company exists, active, not in bankruptcy | KRS `dzial1`, `dzial6`; VAT white list | Document-verified |
| Who can sign for the company | KRS `dzial2` | Document-verified |
| What it does | KRS PKD codes, website | PKD = document-verified; website = declared |
| Financial standing (Art. 41) | Filed statements, arrears (`dzial4`) | Document-verified |
| Past performance (Art. 42) | TED awards | Document-verified |
| Foreign ownership | GLEIF parents, KRS shareholders | Document-verified |
| Sanctions | Sanctions lists | Document-verified (screening, not proof of innocence) |
| Certificates | Uploaded PDF + IAF/OASIS lookup | Declared → document-verified when matched |
| Defence readiness | NCAGE, NSPA registration, past defence awards | Document-verified |
| Free capacity | Supplier agent input | Declared (with date) |

## Rules
- Cache every response with `source_url` and `observed_at`; don't re-fetch on every page view.
- Respect rate limits (VAT white list has daily caps).
- KRS lists board members by name — personal data. Store only what the feature needs;
  don't display it in the demo beyond what's necessary.
- No customer data, nothing classified (challenge ground rule).
