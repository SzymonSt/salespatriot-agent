# Demo plan

Goal: one concrete use case that shows the whole mechanism. Don't build all of Europe's
procurement infrastructure in a day.

## Product: Autonomous Supplier Discovery & Deal Engine
Fictitious buyer and demand (clearly labelled); real public data for profiles and history.

## Demo flows `[TEAM DECISION]` (from the brainstorm)
1. **Onboard a new company from any EU country** — enter a VAT number or national id → agentless
   enrichment (VIES, GLEIF, TED awards + country adapter: PL, CZ, FR live) → profile with evidence levels.
2. **Add an offer/capability to an existing company** → supplier agent re-matches against
   open demand and tenders.
3. **Due diligence** — KYC on onboarding, then per-deal checks (licences, capacity, transport,
   price, references) with verdict GO / GO WITH CONDITIONS / ESCALATE / NO-GO
   ([22](22-kyc-and-deal-due-diligence.md), [15](15-agent-due-diligence-network.md)).
4. **Autonomous deal** — buyer agent posts demand → matching → RFQ → supplier agents quote
   (find partners if needed) → negotiation inside mandates → **deal closed, no human click** →
   transaction record + audit trail.

## Screens
| Screen | Shows |
|---|---|
| Buyer dashboard | Demand, matched suppliers, shortlist, offers, deal status |
| Supplier dashboard | Profile, capabilities, evidence levels, opportunities, RFQs, offers |
| Market intelligence | Graph, supplier relationships, historical procurement (real TED awards) |
| Agent orchestration | Live agent messages, negotiation, audit trail |

## Build priorities
| # | Feature | Importance |
|---|---|---|
| 1 | Ingest + normalise public data (TED, VIES/GLEIF, country registers) | Critical |
| 2 | Supplier profiles + capabilities | Critical |
| 3 | Buyer demand parser | Critical |
| 4 | Semantic supplier matching | Critical |
| 5 | Buyer / supplier agents | Critical |
| 6 | Autonomous deal closure | Critical |
| 7 | Historical intelligence | High |
| 8 | Full knowledge graph | Medium |
| 9 | Advanced demand forecasting | Low for demo |

## Scoring alignment
| Criterion (pts) | How we score |
|---|---|
| Usefulness (30) | Real SME pain (Tier 2–4 visibility, qualification); evidence levels buyers can trust |
| Creativity (30) | Agent-to-agent references; consortium formation; evidence-backed living profile |
| Use of data (20) | Live TED across all EU countries (award winners), live VIES/GLEIF + national registers (PL, CZ, FR), CPV vocabulary |
| Demo quality (20) | One clean end-to-end run, visible agent messages, deal closes on stage |

## Direction ranking (team's product view, not research results)
1. Autonomous supplier matching — 9.5/10
2. Historical procurement intelligence — 9/10
3. Autonomous supplier qualification — 8.5/10
4. Consortium formation — 8.5/10
5. Demand forecasting — 8/10
6. Supply-chain risk intelligence — 8/10

Recommended combo: **historical procurement intelligence + capability graph + autonomous
buyer/supplier agents**.

## Demo risks and mitigations `[OWN KNOWLEDGE]`
- Demo runs on the **host's computer** from the submitted link → deploy it; don't rely on localhost.
- Live APIs can be slow or rate-limited → cache a snapshot, fall back to it silently, but
  show at least one live call.
- LLM non-determinism → fixed seed data, low temperature, mandates that guarantee a deal
  can close within N rounds.
- Never show classified, export-controlled or customer data; label fictitious actors.
