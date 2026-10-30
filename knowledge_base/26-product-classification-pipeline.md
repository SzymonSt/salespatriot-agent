# Product classification pipeline: from TED text and agent listings to families and attributes

How every new item (TED lot, supplier offer, buyer demand, uploaded catalogue line) gets a
product family and a typed attribute set, live. The taxonomy itself is in
[25-product-families.md](25-product-families.md). Status: `[TEAM DECISION — proposed]`.
The database side is **already deployed** in Supabase project `hackathon` (2026-10-30), see §6.
Since the merge of 2026-10-30, database triggers feed `product_items` from the company-profile and TED tables. How the
two layers fit together, and the rules for changing them: [27-ontology-integration-guide.md](27-ontology-integration-guide.md).

## 1. One rule set for both sides
- The same classifier and the same family schema serve supply and demand. Otherwise a demand
  in one family never meets an offer in another.
- Families are assigned **when an item is written**, never at query time. Queries only filter.
- TED gives only CPV codes plus free text for what is bought (no item-attribute fields in eForms,
  [25 §1](25-product-families.md)). So every TED-side attribute is an **extracted claim**.
- Agents that create offers or demands write structured data directly, validated against the
  family schema. Extraction from text is only needed for TED and uploads.

## 2. Unit of classification: one item per lot (or offer line)
**Live sources (2026-10-30).** The bridge triggers (`product_bridge_*`, [27 §3](27-ontology-integration-guide.md))
create items from four tables:

| Source table | `source_kind` / `side` | `source_ref` | Level |
|---|---|---|---|
| `supplier_contracts` (372 rows) | `ted_lot` / supply | `contract:<supplier_id>/<notice_number>/<lot_id or 'notice'>` | **lot** (already parsed from the award XML by `ted_supplier_contracts.py`) |
| `suppliers.catalogue->'items'` (339) | `catalogue` / supply | `catalogue:<supplier_id>/<md5(product, brand_model, cpv)>` | catalogue line |
| `ted_notices`, `kind = 'competition'` (2,230) | `ted_notice` / demand | `ted:<publication_number>` | notice |
| `ted_awards` (1,036, one per winner) | `ted_notice` / supply | `ted:<publication_number>/<winner_key>` | notice × winner |

So the 20 profiled suppliers already have lot-level supply items. XML parsing (below) is only needed for competition
notices and for winners outside those 20 suppliers.

**Target model** (lot-level TED, agent listings, uploads):

| Source | `product_items.source_kind` | `source_ref` | `side` | Item `verification_status` |
|---|---|---|---|---|
| TED competition notice, per lot | `ted_lot` | `ted:<publication-number>/<LOT-id>` | `demand` | `document_verified` (official notice) |
| TED result notice, per lot and winner | `ted_lot` | `ted:<publication-number>/<LOT-id>/<winner key>` | `supply` (org = winner) | `document_verified` |
| TED notice with no lot structure (fallback) | `ted_notice` | `ted:<publication-number>` | as above | `document_verified` |
| Supplier catalogue line (e.g. `data/profiles/suppliers.json` `catalogue.items`) | `catalogue` | `catalogue:<supplier id>/<md5>` (live, see above; position kept in `source_index`) | `supply` | from the item's evidence |
| Offer created by a supplier agent | `offer` | `offer:<offer id>/<line>` | `supply` | `declared` |
| Demand posted by a buyer agent | `demand` | `demand:<demand id>/<line>` | `demand` | `declared` |
| Uploaded historical deal or catalogue | `upload` | `upload:<file hash>/<row>` | either | `declared` until confirmed |

Two different confidence notions, kept apart:
- `verification_status` = how trustworthy the **source** is (a TED award is `document_verified`).
- `classification_method` + `confidence` = how the **family and attributes** were assigned. A
  family guessed by an LLM from an official notice is still a model inference. Its claim is `inferred`.

**Lots need the eForms XML.** The Search API JSON flattens per-lot fields into lists, so title,
CPV, quantity and winner cannot be paired reliably ([24](24-ted-notice-format.md)). Fetch the XML once
per new notice (about 3 notices per working day in our scope) and read `ProcurementProjectLot`
(title, description, main/additional CPV, `EstimatedOverallContractQuantity` + `@unitCode`) and
`NoticeResult` (winners per lot). `ted_supplier_contracts.py` (`lot_details`, `won_lots`) already
does this. Until the XML is parsed, a notice-level item is the fallback.

Text given to the classifier (`product_items.source_text`): lot title + lot description, procedure
title and description for context, all in the original language, plus the English notice title.
Buyer type and CPV codes with labels go in as separate inputs.

## 3. The cascade (cheap → expensive)
| Stage | What | Decides when | `classification_method` | Confidence |
|---|---|---|---|---|
| 0. Noise | `product_noise_rules`: the notice's only focus CPV is a noise code (35341100, 35311400) **and** noise keywords hit | always applied first | `rules` | 0.9 |
| 1. CPV | `product_family_candidates_by_cpv(cpv[])`: `exact` codes decide; `candidate` codes give a short list | one `exact` family and no conflicting `exact` | `cpv` | 0.95 |
| 2. Rules | Score each candidate: calibre parsed from text and its `default_family_id` (+2), keyword hits from `product_family_keywords` (+1 each, max 3), CPV `candidate` (+1), co-occurring `related` CPV (+1) | top score ≥ 3 and ahead of the next by ≥ 2 | `rules` | 0.8 |
| 3. LLM | Classify among the short list (or all families of the class), then extract attributes with the family's JSON Schema | everything else | `llm` | model's own, capped at 0.9 |
| 4. Normalise and validate | Calibre via `product_normalise_calibre`, enums checked, units converted (grains → g, inches → mm), unknown keys → `extra` | always | — | — |

What the 2026-10-30 data says about the split ([25 §2](25-product-families.md)):
- 41–42% of notices carry only a class-level CPV such as 35330000, so stage 1 often stops at the class.
- About half of the lot titles name no product ("Los 1 2023", "Časť 5"). For those, the description has to be read.
- Calibre alone is found in about 26–31% of lot titles.

Expect stage 2 to settle the clear cases (cartridges with a calibre, pistols, swords). The LLM
handles the rest. Measure the split on the backfill and report it (backlog T3).

### LLM contract (stage 3)
- **Call A — classify.** Inputs:
  - `source_text`, CPV codes with labels, buyer type;
  - the candidate families with their description, includes, excludes and the boundary rules
    from [25 §5](25-product-families.md).

  Output (structured): `{family_id: <enum of candidates + out_of_scope.* + "none">, confidence: 0–1, evidence: "<exact quote>"}`.
- **Call B — extract.** Schema = `product_family_schema(family_id, 'extraction')`, plus line fields:
  - `quantity` (number), `quantity_max` (number), `unit` (`H87` | `KGM` | `MTR` | `LTR`).

  Output: `{attrs, extra, quantity, unit}`.
- Rules in the prompt:
  - quote, don't guess;
  - no value that the text does not state;
  - never derive calibre from a weapon model name;
  - frameworks: maximum quantity in `quantity`, minimum in `extra`;
  - several products in one lot → choose the dominant one and list the others in `extra.other_products`.
- Use structured outputs or tool use with strict schemas. Model choice, Batches API for the
  backfill, and handling of `stop_reason == "refusal"` (weapons topic) follow `PLAN.md` §4.
  A refusal or an invalid answer after one retry → `family_id = null`, `needs_review = true`.

### Thresholds
| Result | Action |
|---|---|
| confidence ≥ 0.8 | accept |
| 0.5 ≤ confidence < 0.8 | accept, `needs_review = true` |
| < 0.5, or `"none"` | keep `class_id`, `family_id = null`, `needs_review = true` → input for the proposal job (§5) |
| family has `review_flag`, or a family rule fires (e.g. anti-personnel mine) | accept, but any deal is ESCALATE ([22](22-kyc-and-deal-due-diligence.md)) |

## 4. Writing results
1. **Claims** (table `claims`, [10](10-ontology-data-model.md)):
   - one `HAS_FAMILY` claim per item;
   - one `attr:<key>` claim per attribute value.

   Common fields: `subject = 'item:<uuid>'`, `source_url` = TED notice URL or listing id,
   `extractor_version`, `taxonomy_version`. Evidence level:
   - rules or CPV on an official notice → `document_verified`;
   - LLM → `inferred`;
   - agent listing → `declared`.
2. **Current values**: `product_items.attrs` = per key, the claim with the best evidence
   (`independently_verified` > `document_verified` > `peer_attested` > `declared` > `inferred`), then
   highest confidence, then newest. Set `processed_hash = text_hash`, `taxonomy_version`, `extractor_version`.
3. **Derived tags**: add cpvsuppl tags to `item_classifications` by the rules in
   `product_cpvsuppl_tags` (e.g. FB02 for every weapons/ammo item, FB01 when `end_use` has `military`).
4. **Regulatory defaults**: the family crosswalk (EU Military List, HS, ADR) gives defaults for
   D2/D3/D8. Item-level values stated in the text (`un_number`, `adr_classification`) override them.
5. **Downstream**:
   - rebuild the item's `knowledge_chunks` row: deal card with `Family:` and the canonical attributes in the text, plus a `family_id` column ([23](23-self-learning-rag.md));
   - re-match: a new supply item → open demands in the same family; a new demand item → `product_match_supply`.

## 5. Live updates and taxonomy changes
Live flow since the merge of 2026-10-30 (details and object status: [27 §3, §7](27-ontology-integration-guide.md)):
```
ted-sync (hourly, pg_cron)            supabase_sync.py (profiles)            agent listings / uploads (planned, T8)
   │ upsert ted_notices, ted_awards      │ upsert suppliers, buyers;              │ insert product_items (validated)
   │                                     │ delete + re-insert supplier_contracts  │
   ▼                                     ▼                                        ▼
product_bridge_* triggers (never fail the write) ──► product_items upsert on (source_kind, source_ref) ──► text_hash changes
                                                     ▼
          product_items_pending (view) ──► product_classify_pending() (SQL, pg_cron 'product-classify', every 10 min)
                                                     ▼                                + LLM pass for undecided items
                            claims → attrs → tags ──► ui_* views ──► web/ontology.js
                                                     ▼  (planned)
                                      knowledge_chunks → re-match → notify agents
```
- **Deletes in the sources do not delete items.** `product_bridge_cleanup_orphans()` (hourly) removes items whose
  source row has been gone for more than 1 hour. This is what makes the DELETE + re-INSERT of `supabase_sync.py` safe.
- Stages 0–2 run as SQL functions, not as the Edge Function first planned in T3.
- **Idempotent.** An item is pending only when its text changed (`processed_hash <> text_hash`) or its taxonomy version is old. Re-running does nothing else.
- **Corrigenda** update the same `source_ref`. The new text changes the hash, and the item is reclassified.
- **Taxonomy change** = PR to `data/families.yaml` + `taxonomy_version` bump + `python3 seed_product_taxonomy.py` → SQL.
  - Then reclassify only what the change touches: items in changed families, items with `family_id` null, or items with the new key in `extra`.
  - Use stored text only, with no new TED calls. Do both sides before matching switches to the new version.
- **Proposal job** (nightly) writes to `product_taxonomy_proposals`. A person decides; nothing changes the schema automatically ([10](10-ontology-data-model.md), [12](12-historical-intelligence.md)).

  | Signal | Proposal kind |
  |---|---|
  | ≥ 10 items with the same key in `extra` within one family | `new_attribute` |
  | ≥ 5 items with the same unknown enum value | `new_enum_value` |
  | ≥ 3 sightings of a calibre spelling that does not resolve | `new_alias` / new calibre |
  | ≥ 20 items in a class with `family_id` null that cluster (CPV, keywords, embeddings) | `new_family` |
  | A family with ≥ 30 items whose attributes split into two stable groups, with different buyers or suppliers | `split_family` |
  | Two families that rarely reach 30 items a year and share attributes | `merge_families` |

## 6. What is deployed in Supabase (project `hackathon`)
Migrations (also in `supabase/migrations/`): `20261030125925_product_taxonomy`, `20261030130820_product_taxonomy_epo_alignment`,
`20261030133533_product_items_source_links` (adds `supplier_id`, `buyer_id`, `source_table`, `source_pk`, `source_index`,
`source_seen_at` to `product_items`). The bridge, classifier and UI objects added by the merge are listed with their
status in [27 §7](27-ontology-integration-guide.md).

| Object | Purpose |
|---|---|
| `product_taxonomy_versions` | One row per released taxonomy version |
| `product_families` | Level 1 classes and level 2 families, with includes, excludes, rules, evidence, `in_focus`, `review_flag`, `scheme_uri` (`urn:salespatriot:product-family:<id>`) |
| `product_attribute_defs` | Global attribute dictionary: one meaning per key |
| `product_family_attributes` | Which attributes a family has; `required` (listings only), `is_match_key`, narrowed enums |
| `product_family_crosswalk` | Mappings to CPV, cpvsuppl, EU Military List, NATO FSC, HS, Firearms Directive, ADR, STANAG |
| `product_family_keywords` | Multilingual regex stems for stage 2 |
| `product_calibres`, `product_value_aliases` | Calibre reference and spelling aliases |
| `product_noise_rules`, `product_cpvsuppl_tags` | CPV noise codes; cpvsuppl tagging rules |
| `product_items` | Classified items, both sides; `attrs` JSONB with GIN + calibre index; `text_hash` generated; links to `suppliers` / `buyers` and to the source row |
| `claims` | Shared claim table ([10](10-ontology-data-model.md)); attribute-level evidence |
| `product_taxonomy_proposals` | Human-approved change queue |
| `product_family_tree` (view) | Families with own attributes and CPV codes |
| `product_items_pending` (view) | Work queue for the classifier |
| `product_family_attribute_list(id)` | Own + inherited (class-level) attributes |
| `product_family_schema(id, mode)` | JSON Schema; `mode = 'extraction'` (nothing required) or `'listing'` (required enforced) |
| `product_family_candidates_by_cpv(cpv[])` | Stage 1 |
| `product_normalise_calibre(text)` | `'9 mm Parabellum'` → `9x19`, `'7,62mm x 51'` → `7.62x51` |
| `product_match_supply(demand_item, limit)` | Same family, match-key attributes must agree, ranked by key matches and evidence |
| `product_bridge_*` (5 triggers, functions, cron `product-bridge-cleanup`) | Feed `product_items` from `supplier_contracts`, `suppliers.catalogue`, `ted_notices`, `ted_awards`; backfill; orphan cleanup. Migration `20261030134613_product_bridge` |
| `ui_items`, `ui_supplier_families`, `ui_tender_families`, `ui_match_tender()`, `ui_regulatory_profile()`, `ui_name_key()` | Read API for the web UI (anon). Migrations `20261030134817_ui_data_api`, `20261030135241_ui_data_api_refine`, `20261030135639_ui_data_api_company_key`, `20261030135932_ui_data_api_match_speed` |
| `product_classify_item()`, `product_classify_pending()`, `product_classify_calibres()`, cron `product-classify` | Stages 0–2 in SQL (`rules-v1`), every 10 min. Migration `20261030140649_product_classifier_rules` |

RLS: public read for the taxonomy and for `product_items` / `claims` rows with `visibility = 'public'`.
Writes go through the service role (Edge Functions, scripts).

```sql
select * from product_family_tree order by sort_order;
select product_family_schema('ammunition.small_calibre');              -- extraction schema
select product_family_schema('ammunition.small_calibre', 'listing');   -- calibre required
select * from product_family_candidates_by_cpv(array['35331500','35330000']);
select product_normalise_calibre('5,56 x 45 mm NATO');                  -- 5.56x45
select * from product_match_supply('<demand item uuid>', 20);
```

## 7. Agent tools (thin wrappers over SQL / the Edge Function)
| Tool | Does | Backed by |
|---|---|---|
| `list_families(class?)` | Families with descriptions and boundaries | `product_family_tree` |
| `get_family_schema(family_id, mode)` | JSON Schema for extraction or listing | `product_family_schema` |
| `classify_item(text, cpv[], buyer_type?)` | Dry-run of the cascade; returns family, attrs, confidence, evidence; writes nothing | `product-classify` (dry-run flag) |
| `create_listing(side, family_id, attrs, quantity, unit, org_id)` | Validates against `product_family_schema(…, 'listing')`, normalises, inserts | insert into `product_items` + claims (`declared`) |
| `find_supply(demand_item_id)` | Candidates with KYC status and evidence | `product_match_supply` + KYC tables ([22](22-kyc-and-deal-due-diligence.md)) |
| `propose_taxonomy_change(kind, payload, example_item_ids)` | Files a proposal; never edits the taxonomy | `product_taxonomy_proposals` |

## 8. Backlog (implementation order, each with a done check)
| # | Task | Done when |
|---|---|---|
| T1 | Taxonomy deployed and seeded from `data/families.yaml` | `select count(*) from product_families` = 40 (5 classes + 35 families). **Done 2026-10-30** |
| T2 | Lot items from TED XML (both kinds, last 365 days, then live) | Every focus notice has lot-level `product_items` rows; notice-level stubs removed. **Partly done 2026-10-30:** the bridge gives lot-level supply items for the 20 profiled suppliers (`supplier_contracts`, 372); competition notices and other winners are still notice-level, see [27](27-ontology-integration-guide.md) |
| T3 | Stages 0–2 as an Edge Function `product-classify` (TypeScript, shares regexes with the DB tables) | Backfill report: share decided by noise / CPV / rules, per class. **In progress 2026-10-30:** built as SQL functions `product_classify_*` + pg_cron instead of an Edge Function (live); first run on all 3,977 items: 420 decided by CPV, 767 by rules, 2,790 undecided; the per-class report is pending, see [27](27-ontology-integration-guide.md) |
| T4 | Stage 3 (LLM, two calls, strict schemas, refusal fallback) | Every item has a family or `needs_review`; a person labels 100 random items: family precision ≥ 90%, calibre precision ≥ 95%. **In progress 2026-10-30** (LLM pass for undecided items, `llm-agent-v1`), see [27](27-ontology-integration-guide.md) |
| T5 | Claims + `attrs` materialisation (SQL function `product_item_refresh_attrs(item_id)`) + cpvsuppl tags | `attrs` equals the best claim per key on a sample. **In progress 2026-10-30** (classifier writes claims, attrs and tags), see [27](27-ontology-integration-guide.md) |
| T6 | Import the 339 catalogue items from `data/profiles/suppliers.json` (`category`, `cpv`, `specs`, `evidence`) | 339 supply items; spec keys mapped to the dictionary; leftovers in `extra`. **Partly done 2026-10-30:** 339 items created by the bridge from `suppliers.catalogue` (verified); spec-key mapping is part of the classifier, see [27](27-ontology-integration-guide.md) |
| T7 | Hook into `ted-sync`: trigger or end-of-run call creates stubs; classifier cron every 10 min | A new TED notice is classified within 15 minutes of sync. **Partly done 2026-10-30:** bridge triggers on `ted_notices` / `ted_awards` create the items (live); classifier cron `product-classify` live too; the 15-minute check is not yet measured, see [27](27-ontology-integration-guide.md) |
| T8 | Agent tools of §7, with listing validation | Buyer agent posts a demand with `calibre` missing → rejected with the schema error |
| T9 | `knowledge_chunks.family_id` + deal-card text with family and canonical attributes ([23](23-self-learning-rag.md)) | Hybrid search filtered by family returns the expected cards |
| T10 | Nightly proposal job + review list in the app | Proposals appear with counts and examples; approving one leads to a PR |
| T11 | D2/D3/D8 defaults from the crosswalk ([22](22-kyc-and-deal-due-diligence.md)) | A deal in `ammunition.small_calibre` shows the expected licence and ADR checks. **UI part live 2026-10-30:** `ui_regulatory_profile(family, calibre)` returns the crosswalk and the item rules of [25 §7](25-product-families.md); not yet wired into deal checks, see [27](27-ontology-integration-guide.md) |
| T12 | Metrics: per-family counts, review rate, unresolved calibres, backtest recall@5 by family ([23](23-self-learning-rag.md)) | Numbers visible on the market-intelligence screen |
