# Agent due-diligence network

`[TEAM DECISION]` Agents talk to each other on the platform. Due diligence runs in three layers;
the full category list is in [22-kyc-and-deal-due-diligence.md](22-kyc-and-deal-due-diligence.md):
1. **KYC on joining**: agentless checks on public data and registries (K1–K11).
2. **Monitoring**: registry, sanctions and licence changes between deals.
3. **Per deal** (D1–D14): includes **agent-to-agent reference checks**. The buyer's agent asks
   the agents of the supplier's past customers how the cooperation went, like making a
   reference phone call.

## Reference check protocol (D13)
```
Buyer agent → platform: "reference request about supplier S"
Platform → agents of S's past counterparties (deals on the platform / public TED awards)
Reference agent → structured answer → stored as Claim(peer_attested)
```
Questions are structured, not free-form gossip: delivered on time? (yes / late N days / no) ·
quality issues? (none / minor / major) · full quantity delivered? · price changed after
award? · responsiveness 1–5 · would work again?

## Guardrails `[OWN KNOWLEDGE]`
- **Tied to a real deal**: an agent may vouch only for a counterparty it has a recorded deal or a
  public award with. This blocks fake reviews and fake (Sybil) agents.
- **Consent and scope**: the reference agent answers under its owner's sharing policy. It
  never shares prices, specs or customer data.
- **Weighting**: by the reference's own evidence level and by recency; references lose weight over time.
- **Conflicts of interest**: ignore references from companies linked to the supplier (same owner).
- **Transparency**: the buyer sees each reference with its source; the supplier gets a right of reply.

## Demo
Onboard a supplier → KYC runs live (KRS + VAT white list + TED) → a deal starts → the deal
check runs, including one seeded and clearly labelled reference → verdict → the deal closes.
