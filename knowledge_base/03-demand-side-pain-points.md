# Demand-side pain points (ministries, agencies, primes)

A buyer does not just look for a product. It looks for a way to meet a specific need under
technical, legal and time constraints.

| Pain point | What it means |
|---|---|
| Supplier discovery | Hard to find new suitable suppliers beyond known primes |
| Capability verification | Hard to check that a supplier really has the machines, people, certificates and capacity |
| Fragmented procurement | Different countries, systems, languages and document formats |
| Long procurement cycles | Document analysis, qualification, RFQs and negotiation take a long time |
| Supply-chain risk | Unknown if the supplier depends on one component source or a risky country |
| Capacity uncertainty | Unknown if the firm can deliver by the deadline |
| Supplier concentration | Too much dependence on one maker or region |

## 1. Qualification — finding a firm is only step one
Example: an agency needs 500 specific electronic components. A search finds 20 firms.
The buyer then asks:
1. Does the maker have the technical capability?
2. Does the product meet the spec?
3. Can it deliver the quantity?
4. Does it hold the required certificates?
5. Is it acceptable on security and origin grounds?
6. Has it done similar orders?
7. Can it deliver on time?

A company search answers only question 1. Agents should produce **evidence-backed supplier
profiles**: every claim linked to a source and a confidence level (see
[10-ontology-data-model.md](10-ontology-data-model.md)).

## 2. Geographic and language fragmentation
- Germany, France, Poland etc. each publish on their own platforms.
- Poland: e-Zamówienia (national) and the Armament Agency (AU).
- NATO: NSPA. EU: TED. EDA publishes its own procurement.
- No single place shows the whole market.

**Agent jobs:** cross-country discovery, automatic translation, CPV and terminology
normalisation, similar-tender detection, finding suppliers in other countries.

## 3. Single-source dependency
If one maker supplies a component and it loses raw materials, has production problems, is
acquired, loses a key subcontractor or gets into financial trouble, a whole programme can stall.

Real example `[VERIFIED 2026-10-30]`: TED notice **419618-2026** — Agencja Uzbrojenia buys
12 sets of the BSP-U GLADIUS system (deliveries 2027–2030) by negotiated procedure without
prior publication. The notice names **WB Electronics S.A.** as the "one known contractor"
capable of performing it, citing exclusive rights, supplier-owned technical documentation,
incompatibility of a change of supplier, and urgency under the EU SAFE instrument.
This is a normal, legal pattern — and exactly the kind of concentration a risk agent should
surface.

**Agent opportunity:** propose alternative sources and flag concentration risk
(e.g. "CPV class X: 1 winner across 12 awards in 3 years").

Caveat: few visible winners ≠ no competition. Separate (a) legal single-source procurement,
(b) limited competition, (c) missing data.
