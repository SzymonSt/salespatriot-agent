# Agent due-diligence network

## Team decision (from the brainstorm) `[TEAM DECISION]`
1. Agents talk to each other **on the platform**.
2. Due diligence runs in two steps:
   - **first on public data** — collected agentless (registries, public results, online
     ratings, company history) — see [07-company-data-sources.md](07-company-data-sources.md);
   - **then through other agents** that have dealt with the company before — like calling
     someone who bought from them ("reference call").
3. The buyer's agent can ask the agent of a company that previously bought from a supplier
   what that experience was like.

## Step 1 — public-data check (agentless)
Plain code, runs on onboarding and on schedule:
KRS (exists, active, board, bankruptcy) → VAT white list (active VAT, bank account) → TED
awards (past performance) → GLEIF (parent company) → sanctions screening → certificates
lookup → website / reviews (low confidence).
Output: claims with `document_verified` or `declared` status and a short risk summary.

## Step 2 — agent-to-agent reference check
```
Buyer agent ──"reference request about Supplier S"──> Platform
Platform ──finds agents of past counterparties of S (from deals on platform / public awards)──>
Reference agent(s) ──structured answer──> Buyer agent
```
Structured questions (no free-form gossip):
- delivered on time? (yes / late by N days / no)
- quality issues? (none / minor / major)
- quantity met? price changed after award?
- communication / responsiveness (1–5)
- would work again? (yes / no)

Stored as claims with `verification_status = peer_attested`, `source = agent:<id>`,
`observed_at`, and a link to the deal they refer to.

## Guardrails `[OWN KNOWLEDGE]`
- **Deal-anchored only**: an agent can attest only about a counterparty it has a recorded deal
  with (on the platform) or a public award linking them. Stops fake reviews and Sybil agents.
- **Consent and scope**: the reference agent answers under its owner's sharing policy; it
  never shares prices, specs or other confidential terms of its own deals. No customer data
  (challenge rule).
- **Weighting**: weight answers by the reference's own evidence level and by recency; decay
  over time.
- **Conflicts of interest**: ignore references from companies linked to the supplier (same
  parent in GLEIF/KRS).
- **Show, don't hide**: the buyer sees each reference with its source, not just a score.
- **Right of reply**: the supplier agent can respond to negative references.

## Why it matters
Public data shows what a firm *did*; references show *how well*. This is the closest
software equivalent of the phone calls buyers make today — and it gets better with every
deal on the platform (network effect).

## Demo idea
Onboard a supplier → public check runs (live KRS + TED) → buyer agent asks for references →
one seeded "past customer" agent answers → evidence panel updates → deal proceeds.
Clearly label seeded/fictitious actors in the demo.
