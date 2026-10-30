# KYC at onboarding vs due diligence per deal

`[TEAM DECISION]` Static checks run **once, when a company joins the platform** (KYC).
Checks specific to one transaction run **inside each deal** (deal due diligence).
**Monitoring** in between keeps KYC fresh. Legal and registry facts below are
`[OWN KNOWLEDGE — verify]` unless marked.

## Three layers
| Layer | When | Question it answers | Runs as |
|---|---|---|---|
| **1. KYC** | On joining; full refresh every 90 days or on a trigger | Who are you, and may we trade with you at all? | Agentless pipeline ([07](07-company-data-sources.md)) |
| **2. Monitoring** | Daily/weekly jobs | Has anything changed since KYC? | Agentless jobs + alerts |
| **3. Deal DD** | Every deal, before an agent may accept | Can **this** supplier deliver **this** item to **this** buyer — legally, on time, at a sensible price? | Deal agent + rule tables + peer references ([15](15-agent-due-diligence-network.md)) |

Check types: **HARD** = gate (fail → deal blocked, or escalated to a human who can fix it) ·
**SCORE** = lowers rank/confidence or adds a contract condition · **INFO** = shown only.

## Where the categories come from
| Source in the knowledge base | Categories it drives |
|---|---|
| Directive 2009/81 Art. 39–46 ([05](05-capability-verification.md)) | Exclusion, suitability, financial standing, technical ability, quality |
| NSPA Source File eligibility ([05](05-capability-verification.md)) | Residency, eligibility, present capability, financial standing, past performance |
| UK MOD security of supply ([05](05-capability-verification.md)) | Supply chain, single points of failure, surge, transport alternatives |
| Pain points ([02](02-supply-side-pain-points.md), [03](03-demand-side-pain-points.md)) | Capacity (biggest gap), single source, cash flow |
| Our scope = weapons and ammunition ([21](21-focus-scope.md)) | Licences, end use, transport of explosives |
| Challenge rules ([00](00-challenge-brief.md)) | No ITAR/EAR, classified, CUI or customer data |
| EU SAFE loans (Reg. (EU) 2025/1106) `[VERIFIED 2026-10-30]` | Non-EU control of contractors; ≤35% of component cost from outside EU/EEA-EFTA/Ukraine |

## Layer 1 — KYC at onboarding (static)
| # | Category | What we check | Source | Type |
|---|---|---|---|---|
| K1 | Legal identity | Exists, active, legal form, address | KRS / CEIDG, VAT white list, VIES, LEI | HARD |
| K2 | Insolvency | No bankruptcy, liquidation or restructuring; arrears | KRS sections 6 and 4 | HARD / SCORE |
| K3 | Authority to sign | The person onboarding and signing the agent mandate is a registered representative or holds a power of attorney | KRS section 2 | HARD |
| K4 | Ownership and control | Beneficial owners, parent company, non-EU control | CRBR (PL beneficial-owner register), GLEIF, KRS shareholders, TED `winner-owner-nationality` | HARD if a sanctioned owner, else SCORE |
| K5 | Sanctions and debarment | Company, owners and board vs EU consolidated list / OpenSanctions; exclusion from procurement | Sanctions lists | HARD |
| K6 | Tax and payout account | Active VAT; payout account on the white list (in PL, paying > PLN 15,000 to an unlisted account carries tax risk) | VAT white list, VIES | HARD for payouts |
| K7 | Sector licences | Arms/ammunition trade licence (PL: MSWiA concession for manufacture of and trade in weapons, ammunition and military goods); certified recipient under Directive 2009/43/EC (EU CERTIDER register) if relevant; NCAGE / NSPA Source File | Uploaded document + public registers where they exist | **HARD for our 4 CPV classes** |
| K8 | Quality certificates | ISO 9001, EN 9100, ISO 14001 with validity dates. AQAP is a contract clause, not a certificate | Upload + IAF CertSearch / IAQG OASIS | SCORE (HARD only if the buyer requires it) |
| K9 | Track record | Past TED awards: buyers, CPVs, values, countries | TED Search API by `winner-identifier` `[VERIFIED]` | SCORE |
| K10 | Capability baseline | Products and CPVs, PKD codes, sites, declared capacity | Supplier input, KRS PKD, TED CPVs | INFO → SCORE |
| K11 | Agent mandate | Signed limits: categories, max deal value, price and terms, who receives escalations | Platform | HARD |

Result per company: `kyc_status = verified | limited | blocked`, an evidence table, `kyc_checked_at`.

## Layer 2 — Monitoring (between deals)
| Trigger | Action |
|---|---|
| Sanctions list update hits the company or an owner | Freeze all open deals → human review |
| KRS change: bankruptcy, liquidation, new board | Re-run K1–K4; freeze on HARD fail |
| Licence or certificate expires in < 30 days | Warn; block deals that need it after the expiry date |
| New TED award or contract modification | Update track record; a large new award can eat free capacity |
| Negative peer reference | Lower reputation score; supplier gets a right of reply |
| Capacity statement older than N days | Supplier agent asked to refresh; until then capacity counts as unknown |

## Layer 3 — Due diligence per deal
Output: **GO / GO WITH CONDITIONS / ESCALATE / NO-GO**, with evidence per category.

| # | Category | What the deal agent checks | Why | Type |
|---|---|---|---|---|
| D1 | KYC freshness | Both sides `verified`, checked < 90 days ago; sanctions re-screened now | Status can change after onboarding | HARD |
| D2 | Is the item allowed for this buyer? | Map CPV → EU Common Military List category (e.g. ML1 small arms, ML3 ammunition); military vs civilian firearm; the buyer may legally acquire it; destination embargoes | Selling a controlled item to an unauthorised buyer is illegal | HARD |
| D3 | Licences for this flow | Domestic trade (concession); intra-EU transfer licence (Directive 2009/43/EC); export outside the EU (Common Position 2008/944/CFSP); civilian firearms export (Reg. 258/2012); transit permits; end-user certificate; broker registration for intermediaries (Common Position 2003/468/CFSP) | Without the right licence the goods cannot ship | HARD |
| D4 | Data rules | No ITAR/EAR content, nothing classified, no CUI, no customer data in the deal record | Challenge rule ([00](00-challenge-brief.md)) | HARD |
| D5 | Technical fit | Spec (calibre, standard, variant), quality regime (AQAP clause, lot acceptance tests), certificates valid at the delivery date | A cheap offer that fails acceptance is worthless | HARD on mandatory specs, SCORE otherwise |
| D6 | Capacity and schedule | Quantity vs dated free capacity; lead time vs deadline; other open commitments on the platform; quantity vs the supplier's largest past delivery on TED | Certificates ≠ capacity, the biggest gap in [05](05-capability-verification.md) | SCORE; HARD if the deadline is impossible |
| D7 | Supply chain and origin | Critical inputs (propellant, primers, explosives, brass/copper, steel); single-source sub-suppliers; non-EU share of component cost | Security of supply; SAFE-funded buyers need ≤35% non-EU components | SCORE; HARD if the buyer's funding requires it |
| D8 | **Transport and logistics** | Ammunition and explosives are dangerous goods, ADR Class 1 (small-arms cartridges are often UN 0012, 1.4S) → ADR-certified carrier; route and transit countries (transit permits); Incoterms (who carries risk); access to the delivery site (military base); storage licence at the receiving site; packaging and marking; transport time inside the lead time | Transport and transit permits are a common hidden blocker and add weeks | HARD if carrier or permit is missing; SCORE on time and cost |
| D9 | Price sanity | Unit price vs history (TED award values in the same CPV) and vs other offers; outliers too low (risk of non-delivery) or too high; total within the buyer's mandate | Does the deal make sense? | SCORE; HARD over the mandate |
| D10 | Payment and financial capacity | Deal value vs supplier size or turnover; advance payment needed; bid/performance guarantees; payment terms (60+ days is normal) | A firm can win a contract and still fail to finance it ([02](02-supply-side-pain-points.md)) | SCORE → conditions (e.g. guarantee) |
| D11 | Contract terms | Incoterms, warranty, penalties, acceptance testing, rights to technical documentation (cf. the GLADIUS sole-source case in [03](03-demand-side-pain-points.md)), offset, governing law | Avoid lock-in and disputes | SCORE → conditions |
| D12 | Integrity | Related parties (same owner on both sides); unexplained intermediaries; orders split below thresholds; declared data contradicting observed data | High diversion and corruption risk in the arms trade | HARD on related party or unknown broker; SCORE otherwise |
| D13 | Reputation | Peer references tied to real deals; past TED contract modifications; disputes | Public data shows *what*; references show *how well* | SCORE |
| D14 | Mandate fit | Deal sits inside both agents' mandates (value, category, terms, minimum evidence level) | Autonomy is allowed only inside the mandate ([14](14-agents-and-autonomy.md)) | HARD → escalate to a human |

## Decision rule
- Any HARD fail → **NO-GO**, or **ESCALATE** if a human can fix it (e.g. upload the missing licence).
- All HARD gates pass and score ≥ threshold → **GO**: the agent may close on its own.
- All HARD gates pass, with open items (guarantee, fresh capacity, named ADR carrier) →
  **GO WITH CONDITIONS**: the conditions are written into the contract draft.
- Every check is stored as a Claim with source and timestamp ([10](10-ontology-data-model.md)).

## What we can check today vs later
| Checks | Demo (today) | Later |
|---|---|---|
| K1–K3, K6 | Live KRS + VAT white list `[VERIFIED]` | CEIDG, other EU registries |
| K4, K5 | KRS shareholders, GLEIF, OpenSanctions | CRBR, continuous screening |
| K7, K8 | Uploaded document + manual flag | Registry lookups where public |
| K9, D9 | TED Search API on our 4 CPVs `[VERIFIED]` | Unit prices from tender documents |
| D2, D3, D8 | Static rule tables: CPV → ML category → required licence; CPV → ADR class; route → transit countries | Licence and permit registries, carrier marketplace |
| D6 | Supplier agent's capacity statement | Production data integrations |
| D13 | Seeded peer references (labelled) | Real reference network |

## Demo idea
Run two deals. In the first, D8 finds no ADR carrier → **GO WITH CONDITIONS**, and the agent
writes the carrier requirement into the contract. The second is a clean **GO** that closes
autonomously. This shows the due diligence actually changes outcomes, not just decorates the screen.
