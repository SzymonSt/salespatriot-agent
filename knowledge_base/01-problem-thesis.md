# Problem thesis

`[HYPOTHESIS]` — team framing from the brainstorm, refined.

## One-line thesis
The European defence supply chain does not lack suppliers. It cannot turn scattered demand
and scattered production capacity into concrete orders that can actually be delivered.

So we do **not** build another tender search portal. We build an **intelligence and
orchestration layer** that understands both what the buyer wants and what a supplier can
really deliver, and lets agents close the gap.

## Five market problems → what we build
| Market problem | Our answer |
|---|---|
| Fragmented information (countries, portals, languages) | One supply-chain knowledge graph over a canonical data model |
| Invisible capabilities (SMEs, Tier 2–4) | Supplier capability intelligence with evidence levels |
| Unpredictable demand | Historical procurement intelligence (who buys what, how often) |
| Slow procurement (weeks of email) | Buyer and supplier agents that run RFQ → quote → negotiation |
| Unused production capacity | Autonomous matching, incl. consortium proposals |

## Three product layers
1. **Supply-chain data infrastructure** — collectors, API, normalisation, CPV, identifiers.
2. **Supply-chain intelligence** — RAG, embeddings, historical patterns, ontology / graph.
3. **Agentic marketplace** — buyer agents, supplier agents, matching, messaging, deals.

Each layer has value alone; together they form a system that learns the market.

## Where the moat is
- Tender matching alone is easy to copy.
- A graph of European production capabilities, supplier relationships and historical demand,
  enriched by every deal on the platform, is hard to copy.
- Public data will **not** show current free capacity or most subcontracting links. The
  marketplace must collect those directly from suppliers (via their agents) and keep them fresh.

## Two-sided learning loop
Buyer posts demand → supplier agents add capability and capacity data → each match, quote,
deal and post-deal reference improves the next match.

## What we say on stage
"We are not building a marketplace where people browse offers. We are building a market
where agents discover collaboration opportunities, understand requirements and close deals."
