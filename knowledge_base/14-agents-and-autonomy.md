# Agents and autonomy

Not every step needs an agent. Use plain code where data is structured; use an LLM where
data is unstructured or reasoning is needed.

## Level 1 — Intelligence agents (high autonomy; public data, produce recommendations)
| Agent | Job |
|---|---|
| Tender Discovery | Watch sources, detect new procedures |
| Tender Understanding | Read documents, extract requirements |
| Supplier Discovery | Find firms that fit a need |
| Supplier Profiling | Build capability profiles with evidence |
| Historical Intelligence | Analyse past procurement |
| Opportunity | Spot new business opportunities |

## Level 2 — Coordination agents (high autonomy) — the real marketplace value
**Buyer agent**: receive demand → understand technical requirements → query graph → find
candidates → compare capabilities → find missing competences → find firms that fill gaps →
build shortlist and RFQs.

**Supplier agent**: receive company profile → understand capabilities → watch new demand →
find matching opportunities → check qualification requirements → list missing documents →
find partners → draft a cooperation proposal.

**Collaboration agent**: buyer needs mechanical production + electronics + assembly; no single
firm has all three → agent finds three complementary firms and proposes a **consortium or
subcontracting chain**. This answers "forming a team takes weeks of emails, calls and PDFs".

## Level 3 — Transaction agents (autonomy with limits)
Can do automatically: prepare RFQ, send it to matched firms, collect quotes, compare price /
lead time / terms, detect mismatches, counter-offer, draft the order.

Must **not**:
- confirm unverified supplier capabilities on its own;
- make binding offers without a mandate;
- approve security requirements or certifications;
- make opaque qualification decisions;
- bypass procurement procedures.

## Autonomy boundary
| Process | Autonomy | Why |
|---|---|---|
| Tender monitoring | Full | Public, repetitive data |
| Requirement extraction | Full + validation | Documents need interpretation |
| Supplier matching | Full | Score is computable |
| Supplier profiling | Full + evidence | Public sources |
| Shortlist | Full | Criteria-based |
| Sending RFQs | Limited | Needs a mandate |
| Collecting quotes | Full | Communication automation |
| Negotiation | Limited | Limits and terms set in advance |
| Signing | Approval | Binding commitment |
| Security and compliance | Human support | High cost of error |

## Bounded autonomy (how we satisfy "deal closed with no human in the loop")
Each agent gets a **mandate** set before the run: budget ceiling, allowed categories,
acceptable terms (price, lead time, payment), counterpart requirements (min evidence level).
Inside the mandate the agent closes on its own; outside it, it stops. Every action is logged
with the mandate rule that allowed it.

For the demo, close an autonomous deal on a simple, **non-controlled industrial item or
service** with clear terms and a limited mandate. We do not need to automate a real defence
contract to prove the market works. `[TEAM DECISION]`

## Mandate example `[HYPOTHESIS]`
```json
{
  "agent": "buyer",
  "category_cpv": ["44530000"],
  "max_unit_price_eur": 4.20,
  "quantity": 5000,
  "latest_delivery": "2026-12-15",
  "min_supplier_evidence": "document_verified",
  "max_rounds": 3,
  "may_sign": true
}
```
