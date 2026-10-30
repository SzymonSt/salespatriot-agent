# KYC at onboarding vs due diligence per deal

`[TEAM DECISION]` Scope: **companies from all of Europe**. Every check is defined generically;
national registers plug in as **country adapters** ([07](07-company-data-sources.md)), and Poland is just one of them.
Static checks run **once, when a company joins the platform** (KYC).
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
**Implemented (dd-v1):** `../supplier_due_diligence.py` + `../dd_sources.py`. Score 1–10 in six
categories: identity 15%, ownership 20%, track record 25%, financial 15%, defence credentials 15%,
integrity 10%. Hard gates: sanctions match, RU/BY ownership, insolvency, dissolved company.
Registers covered: CZ, PL, FR, RO, SK, DK, EE, LV + VIES, GLEIF, Wikidata, EU F&T, GDELT. Supplier
documents go in `../data/supplier_evidence/`. The categories map to K1–K10 below; K11 (agent mandate) is still to build.

Each supplier profile in `../data/profiles/suppliers.json` now carries three sections written by that script:
- `due_diligence` — the 1–10 score, six category sub-scores with reasoning, `red_flags`, gates, confidence.
- `kyc` — K1–K11, each `{gate, status, summary, sources, detail}` with status `pass | fail | review |
  needs_upload | pending_platform | info`; plus `kyc_status` (`verified | limited | blocked`), `kyc_checked_at`,
  `refresh_due` (+90 days) and `open_items`. K3 lists registered representatives and the signing rule
  (CZ, PL masked, SK, FR); K6 lists PL bank accounts registered with the tax office (payout must match);
  K7 passes only on licence wording in a register (e.g. CZ Act 38/1994 permit) or a verified upload —
  a NACE 25.40 code alone is `needs_upload`.
- `deal_baseline` — supplier facts the deal agent reuses: D1 freshness dates, D6 largest past lot,
  D9 lot-value baseline per CPV (lot values, not unit prices), D10 revenue/employees vs largest lot,
  D12 trader-not-manufacturer flag, single-bid share and suppliers sharing a corporate owner,
  D13 reference buyers and count of TED contract modifications.
  D2–D5, D7, D8, D11, D14 depend on the specific deal and are not precomputed.

| # | Category | What we check | Source | Type |
|---|---|---|---|---|
| K1 | Legal identity | Exists, active, legal form, address | Home-country register (adapter: PL KRS, CZ ARES, FR SIRENE…), VIES, GLEIF | HARD |
| K2 | Insolvency | No bankruptcy, liquidation or restructuring; arrears | Home-country register / insolvency register (EU interconnection via e-Justice) | HARD / SCORE |
| K3 | Authority to sign | The person onboarding and signing the agent mandate is a registered representative or holds a power of attorney | Home-country register; otherwise an uploaded extract or power of attorney | HARD |
| K4 | Ownership and control | Beneficial owners, parent company, non-EU control | GLEIF parents, TED `winner-owner-nationality`, supplier declaration; national beneficial-owner registers where accessible (limited since the CJEU ruling of 2022) | HARD if a sanctioned owner, else SCORE |
| K5 | Sanctions and debarment | Company, owners and board vs EU consolidated list / OpenSanctions; exclusion from procurement | Sanctions lists | HARD |
| K6 | Tax and payout account | Active EU VAT number; payout account belongs to the company (country rules apply, e.g. PL VAT white list) | VIES + country adapter | HARD for payouts |
| K7 | Sector licences | **Home-state authorisation** to manufacture or trade in weapons, ammunition and military goods. Every EU country licenses this, and the Firearms Directive (EU) 2021/555 requires dealers to be authorised, but the issuing authority differs (e.g. PL: MSWiA concession; DE: weapons-trade permit, plus a war-weapons permit for military items; FR: AFC authorisation from the Ministry of the Armed Forces). Also: certified recipient under Directive 2009/43/EC (EU CERTIDER register) if relevant; NCAGE / NSPA Source File | Uploaded document + public registers where they exist | **HARD for our 4 CPV classes** |
| K8 | Quality certificates | ISO 9001, EN 9100, ISO 14001 with validity dates. AQAP is a contract clause, not a certificate | Upload + IAF CertSearch / IAQG OASIS | SCORE (HARD only if the buyer requires it) |
| K9 | Track record | Past TED awards (any EU country): buyers, CPVs, values, countries | TED Search API by `winner-identifier` `[VERIFIED]` | SCORE |
| K10 | Capability baseline | Products and CPVs, NACE activity code (25.40 = weapons and ammunition), sites, declared capacity | Supplier input, register activity code (PKD/NAF/… → NACE), TED CPVs | INFO → SCORE |
| K11 | Agent mandate | Signed limits: categories, max deal value, price and terms, who receives escalations | Platform | HARD |

Result per company: `kyc_status = verified | limited | blocked`, an evidence table, `kyc_checked_at`.

## Layer 2 — Monitoring (between deals)
| Trigger | Action |
|---|---|
| Sanctions list update hits the company or an owner | Freeze all open deals → human review |
| Register change (any country adapter): bankruptcy, liquidation, new board | Re-run K1–K4; freeze on HARD fail |
| Licence or certificate expires in < 30 days | Warn; block deals that need it after the expiry date |
| New TED award or contract modification | Update track record; a large new award can eat free capacity |
| Negative peer reference | Lower reputation score; supplier gets a right of reply |
| Capacity statement older than N days | Supplier agent asked to refresh; until then capacity counts as unknown |

## Layer 3 — Due diligence per deal
**Implemented (deal-v1):** `../deal_due_diligence.py` + `../deal_rules.py`, run on open tenders × proven
suppliers. What each check uses today:
D1 KYC age + live sanctions re-screen (company and corporate owners) · D2 CPV → Military List / Firearms
Directive category, state vs non-state buyer (TED `main-activity`), EU arms-embargo list · D3 licence list for
the flow (domestic / intra-EU transfer / non-EU export, transit countries from a land-border graph, end-user
certificate, broker registration for traders) · D4 whole-word scan of the tender text for ITAR/EAR designations
and classification markings in 8 languages + TED `security-clearance-lot` · D5 CPV match level + certificate
validity at the estimated delivery date · D6 tender value vs largest past lot · D7 SAFE funding detection
(TED `eu-funds-*`), supplier/owner origin · D8 ADR class from CPV, land route or sea/air, Channel Tunnel rule ·
D9 award-value distribution in the same CPV from Supabase `ted_notices` · D10 value vs revenue + TED
`guarantee-required-lot` · D11 framework/duration/penalties from the notice + standard clauses · D12 shared
owners among candidates, trader flag, single-bid share, repeated small orders under the threshold · D13 prior
deliveries to the same buyer / reference buyers · D14 Supabase `agent_mandates` or the default policy.
Still missing: real offer price, quantity and delivery date from the negotiation (until then they are conditions),
peer references, carrier marketplace.

Output: **GO / GO WITH CONDITIONS / ESCALATE / NO-GO**, with evidence per category.

| # | Category | What the deal agent checks | Why | Type |
|---|---|---|---|---|
| D1 | KYC freshness | Both sides `verified`, checked < 90 days ago; sanctions re-screened now | Status can change after onboarding | HARD |
| D2 | Is the item allowed for this buyer? | Map CPV → EU Common Military List category (e.g. ML1 small arms, ML3 ammunition); military vs civilian firearm; the buyer may legally acquire it; destination embargoes | Selling a controlled item to an unauthorised buyer is illegal | HARD |
| D3 | Licences for this flow | Licences in the seller's and buyer's countries; intra-EU transfer licence (Directive 2009/43/EC); export outside the EU (Common Position 2008/944/CFSP); civilian firearms export (Reg. 258/2012); transit permits; end-user certificate; broker registration for intermediaries (Common Position 2003/468/CFSP) | Without the right licence the goods cannot ship | HARD |
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
| K1–K3, K6 | Live VIES (all EU) + adapters PL (KRS, VAT white list), CZ (ARES), FR (Recherche d'entreprises) `[VERIFIED]` | More country adapters |
| K4, K5 | GLEIF, TED owner nationality, EU sanctions list | Beneficial-owner registers, continuous screening |
| K7, K8 | Uploaded document + manual flag | Registry lookups where public |
| K9, D9 | TED Search API on our 4 CPVs `[VERIFIED]` | Unit prices from tender documents |
| D2, D3, D8 | Static rule tables: CPV → ML category → required licence; CPV → ADR class; route → transit countries | Licence and permit registries, carrier marketplace |
| D6 | Supplier agent's capacity statement | Production data integrations |
| D13 | Seeded peer references (labelled) | Real reference network |

## Demo idea
Run two deals. In the first, D8 finds no ADR carrier → **GO WITH CONDITIONS**, and the agent
writes the carrier requirement into the contract. The second is a clean **GO** that closes
autonomously. This shows the due diligence actually changes outcomes, not just decorates the screen.
