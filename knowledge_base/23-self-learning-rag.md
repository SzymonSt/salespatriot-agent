# Self-learning knowledge layer (RAG over deals)

`[TEAM DECISION]` The platform learns from deals. Historical public deals (TED awards), live
tenders, deals closed on the platform, and deals companies upload themselves all go into one
knowledge base that agents query through RAG. **More deals on the platform → better
matching, pricing and due diligence.** Every claim of improvement must be measured
(see "Proving it" below), never asserted.

## What goes in
| Source | How it arrives | Becomes | Evidence level |
|---|---|---|---|
| TED award notices, 4 focus CPV classes ([21](21-focus-scope.md)) | Live: backfill once, then incremental sync | Award rows + "deal card" chunks | `document_verified` |
| TED open tenders | Live: incremental sync + "refresh" button | Tender rows + chunks; triggers matching | `document_verified` |
| Platform deals | Automatically when a deal closes | Deal row + deal card + claims (supplier delivered CPV X to buyer Y) | `document_verified`; `peer_attested` after the buyer confirms delivery |
| Deal outcomes | Buyer/supplier agents after delivery: on time, quality, quantity, final price | Outcome row + reference chunk | `peer_attested` |
| Uploaded historical deals | A company uploads its own past contracts (CSV/JSON/PDF) | Deal cards marked as uploaded | `declared` until the counterparty confirms or a document matches |
| Offers and negotiation transcripts | Platform, per deal | Summarised chunk (never raw prices to third parties) | — |
| Knowledge base files (this folder) | Repo | Chunks for rules and definitions | — |

## The deal card (unit of knowledge)
One record per award or deal, stored twice: as **structured rows** (for stats and filters)
and as a **text chunk with an embedding** (for semantic search).
```
DEAL CARD · source: ted | platform | upload · id · date
buyer: name, country, type (MoD, police, agency…) · supplier(s): name, country, size, NACE
item: CPV codes + short description (English) · quantity · unit · total value · currency
procedure: type, legal basis, number of bids if known · delivery: place, lead time, Incoterms
outcome (platform only): on time? quality? final price vs offer? · checks verdict (GO / GO WITH CONDITIONS…)
```

## How accuracy grows with the number of deals
1. **Retrieval priors.** Similar past deals (same CPV class, country, quantity band) are pulled
   into the agent's context → better price expectations (D9), lead times (D6) and typical
   conditions. More deals = denser neighbourhoods = tighter estimates.
2. **Learning-to-rank for matching.** Each match is scored from features: embedding similarity,
   CPV overlap, past awards in the same CPV, country, evidence level, capacity freshness,
   reference score, price position. Start with hand-set weights; every labelled outcome
   (offer accepted / rejected, deal closed, delivered on time) is a training example. Refit
   a simple model (logistic regression first, gradient boosting later) on a schedule.
3. **Evidence upgrades.** A delivered deal turns "supplier claims capability X" (`declared`)
   into a claim backed by a real transaction. Supplier profiles get stronger with every deal.
4. **CPV ↔ capability map.** Which suppliers actually win which CPVs is learned from awards and deals
   rather than hand-written ([08](08-cpv-codes.md) pitfall 4).
5. **Pattern library.** A nightly job proposes patterns with support counts (e.g. "buyer type X
   re-buys class Y every ~12 months"). A human approves each pattern before it enters the
   ontology ([12](12-historical-intelligence.md)).

## Proving it (metrics, not claims)
- **Backtest on public data.** For each TED award in the last 365 days, hide the winner,
  run the matcher on data published *before* the award date, and check whether the real
  winner is in the top-k. Report **recall@5** and **MRR**. Re-run as more deals are ingested →
  a learning curve plotted against the number of deals.
- **Platform metrics:** offer acceptance rate, rounds to close, share of deals that need no
  escalation, error of the price estimate vs the final price, on-time delivery rate.
- Show the curve in the demo only with real numbers from our own runs.

## Guardrails
- **Poisoning:** only deals tied to a real counterparty (platform deal or public award) change
  weights. Uploaded deals stay `declared` and get low weight until confirmed.
- **Privacy:** platform deal terms are visible only to the two parties. Others see aggregates
  only, and only when at least 3 deals fall in a bucket. No customer data in the demo (challenge rule).
- **Recency:** older deals weigh less (time decay); prices drift.
- **Versioning:** keep model and weight versions with every match, so results can be explained and rolled back.
- **Cold start:** public TED history provides the starting knowledge; platform deals refine it.

## Live data adaptation
- **Backfill once** (a few minutes): competition + award notices for the 4 CPV classes via
  `paginationMode: ITERATION` (≤ 250 notices/page, ≤ 10,000 fields/page → ≤ 40 fields at limit 250).
- **Incremental sync** after each OJ S release (~09:00 CET, working days): `PD>=today(-2)`,
  using a 2-day window so overlaps are safe; upsert by `publication-number`; corrigenda mark older versions as not latest.
- **On demand:** a "Refresh from TED" action in the app runs the incremental sync live on stage,
  with a cached snapshot as fallback.
- **Per new notice:** normalise → upsert → resolve organisations → queue KYC enrichment for new
  companies → build the deal card / tender chunk → embed → index → re-match open demand and
  notify supplier agents.

Tables and search function: [13-architecture-pipeline.md](13-architecture-pipeline.md).
