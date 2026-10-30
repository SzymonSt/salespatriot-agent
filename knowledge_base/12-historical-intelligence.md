# Historical procurement intelligence

`[TEAM DECISION]` Aggregate historical tenders and awards; make them searchable for agents
(RAG); detect patterns. `[HYPOTHESIS]` Close patterns into an ontology that grows over time.

Past tenders are not an archive. They record past demand, business relationships, technical
requirements and supplier capabilities.

## What one record gives
| Field | What we can infer |
|---|---|
| Buyer | Who regularly buys what |
| CPV | Which categories are bought |
| Description | Real technical requirements |
| Estimated value / award value | Price ranges |
| Winner | Who won |
| Bidders (if published) | How strong competition was |
| Lot structure | How buyers split large orders |
| Subcontracting info | Disclosed subcontractors (eForms has fields for it) |
| Recurrence | Whether the purchase repeats |

## Five intelligence layers
**A. Buyer intelligence** — what a buyer buys, how often, from whom, at what values; new
categories appearing. → Predict future demand.

**B. Supplier intelligence** — what a firm delivered, in which categories, to which buyers,
in which countries, as winner or subcontractor.
⚠ No public contract ≠ no capability. Only "not confirmed in this dataset".

**C. Procurement pattern intelligence** — repeated demand, repeat buyers, changes in value and
frequency → demand forecasting (with uncertainty: budgets and cycles change).

**D. Supplier relationship intelligence** — the most interesting graph part:
```
Company A → AWARDED → Buyer X
Company A → SUBCONTRACTOR → Company B
```
If B shows up across many projects, we learn its place in the ecosystem.
EDF project participant lists add real consortium links (see [06](06-procurement-data-sources.md)).

**E. Procurement gap intelligence** — categories with few visible suppliers, repeated awards
with little competition, tenders with no award, growing categories.
⚠ Separate single-source procurement, limited competition and plain missing data.

## Value for both sides
| Buyer gets | Supplier gets |
|---|---|
| Purchase history of potential suppliers | Purchase history of potential customers |
| Verified experience | Matched opportunities |
| Past contracts | Historical contract values |
| Alternative suppliers | Customer requirements |
| Concentration insight | Subcontracting entry points |
| Recurring demand | Future demand signals |

## Pattern library (examples — hypothetical, not yet seen in data)
| ID | Type | Pattern |
|---|---|---|
| P001 | Demand | Repeated purchases in a CPV class by the same buyer type |
| P002 | Supplier | Suppliers with similar capabilities win similar tenders |
| P003 | Qualification | Certain tenders repeatedly require the same certificates |
| P004 | Collaboration | Similar tenders involve several contractors / consortia |
| P005 | Pricing | Repeating value ranges for a category |

## Self-learning loop (with guardrails)
New data → agent proposes:
1. Does the pattern fit an existing category?
2. Is it a new variant of an existing pattern?
3. Is it a new relationship?
4. Should existing graph relations be updated?

Observations can be added automatically. **Changes to the ontology itself need validation.**
We do not want an LLM to invent categories after reading a few documents.

## First step with real data `[OWN KNOWLEDGE]`
Pull the 2,703 defence-directive award notices of the last year from TED
(see [09-ted-api-cheatsheet.md](09-ted-api-cheatsheet.md)) → table of
`buyer, winner, country, cpv, value, date` → already enough for layers A, B, C, E in the demo.
