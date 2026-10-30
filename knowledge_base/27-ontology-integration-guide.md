# Ontology integration guide: profile tables, product ontology and the bridge

For the UI and profile-tables owner (web UI, `suppliers`, `buyers`, `supplier_contracts`, the KYC and
due-diligence tables, `supabase_sync.py`), and for anyone who changes those tables after 2026-10-30.

Status:
- The split of ownership and the rules in §4 and §5 are `[TEAM DECISION 2026-10-30]` (merge contract of that day).
- The taxonomy itself is in [25](25-product-families.md); the classification pipeline in [26](26-product-classification-pipeline.md).
- Object names and numbers were checked against the database on 2026-10-30 (afternoon); §7 says what is live and what is
  still planned.

## In one minute
- Your tables stay the source of **company facts**: who a company is, what it won, its KYC and due diligence.
- The product ontology is the source of **product facts**: an item's family, calibre and other attributes, and its regulatory profile.
- Database triggers copy your rows into `product_items` automatically. You call nothing. `supabase_sync.py` works unchanged.
- A classifier runs every 10 minutes and assigns families and attributes.
- The UI reads the results through `ui_*` views. If Supabase does not answer, the UI falls back to the old logic in `web/agents.js`.
- **One rule to remember:** if you change a column or a JSON key that the bridge reads, update the `product_bridge_*`
  function in the same change (§4a).

## 1. What happened on 2026-10-30, and why
**Before:**
- Matching lived in `web/agents.js`: calibre regexes (`calibreKeys`), a CPV prefix test (`isWeapons`, `353…`) and a
  catalogue-category map (`sameFamily`, `CATEGORY_FAMILY`).
- It read only the static JSON in `data/profiles/`. The TED notices and award winners in Supabase were not used for matching.
- The 339 catalogue items used 227 different `specs` keys and up to four spellings of one calibre ([25 §2](25-product-families.md)).

**The change:**
- A product ontology was deployed next to your tables, in the same Supabase project ([25](25-product-families.md)):
  - two levels: 4 CPV classes → 28 families in focus + 7 out of scope (`product_families`: 40 rows incl. the 5 class rows);
  - a fixed attribute schema per family (59 keys, one meaning each);
  - a regulatory crosswalk per family: EU Military List, NATO FSC, HS / CN, Firearms Directive, ADR, STANAG;
  - 57 calibres with spelling aliases and a normaliser.
- A bridge, a classifier and a read API for the UI were built on top of it (§3).

**Why:**
- One shared answer to "what is bought, what is offered" for matching, RAG ([23](23-self-learning-rag.md)) and the agents.
  Without it, every consumer writes its own regexes and they disagree.
- Families make matches precise below the CPV class. The old code compared only the 4-digit CPV class (3533, 3532…),
  so a 9x19 blank or marking round could match a 9x19 ball-ammunition tender. Families and attributes separate them.
- Every product fact carries evidence (a claim with source, method and confidence), as [10](10-ontology-data-model.md) requires.

The taxonomy was approved and is kept as it is. It changes only through a reviewed proposal (§4d).

## 2. Two layers
| | Company layer (yours) | Product layer (ontology) |
|---|---|---|
| Answers | Who is this company? What did it win? Is it safe to deal with? | What is this item? Family, calibre, attributes, regulatory regime? |
| Tables | `suppliers`, `buyers`, `supplier_contracts`, `supplier_due_diligence`, `supplier_kyc`, `supplier_kyc_checks`, `supplier_deal_baseline`, `supplier_documents`, view `supplier_overview` | taxonomy tables `product_*` (reference data), `product_items`, `claims`, `product_taxonomy_proposals` |
| Written by | `supabase_sync.py` (from `data/profiles/*.json`), `supplier_due_diligence.py` | bridge triggers, classifier; later agent listings |
| Source of truth | the JSON build in `data/profiles/` | `data/families.yaml` for the taxonomy; `product_items` + `claims` for item facts |
| Link | — | `product_items.supplier_id` → `suppliers.id` and `buyer_id` → `buyers.id` (both `on delete set null`); `source_table`, `source_pk`, `source_index` point at the exact source row |

The TED layer (`ted_notices`, `ted_awards`, Edge Function `ted-sync`) belongs to a third session. Neither side changes it.

Rule of thumb: a fact about a company goes into your tables. A fact about a product goes into `product_items` and `claims`.
Never copy one into the other. Join on `supplier_id`, `buyer_id` or `publication_number`.

## 3. The bridge and the live flow
```
supabase_sync.py ─► suppliers · buyers · supplier_contracts        ted-sync (hourly) ─► ted_notices · ted_awards
                        │ after insert/update triggers (product_bridge_*)                │ same
                        ▼                                                                ▼
                  product_items   (upsert on source_kind + source_ref; text_hash = md5(source_text))
                        │ text changed or taxonomy newer → view product_items_pending
                        ▼
                  product_classify_pending()   (pg_cron 'product-classify', every 10 min)
                        │ class, family, attrs, confidence, needs_review, claims
                        ▼
                  ui_items · ui_supplier_families · ui_tender_families · ui_match_tender() · ui_regulatory_profile()
                        │ PostgREST with the publishable anon key (web/config.js)
                        ▼
                  web/ontology.js (window.Ontology) ─► app.js, agents.js     (fallback: old local logic)
```

### 3.1 What becomes an item
| Source row | `source_kind` / `side` | `source_ref` (stable key) | `source_table` / `source_pk` | Notes |
|---|---|---|---|---|
| `supplier_contracts` row | `ted_lot` / supply | `contract:<supplier_id>/<notice_number>/<lot_id or 'notice'>` | `supplier_contracts` / the ref tail | `supplier_id` set; title = title, else notice_title, else contract_title; text = title + description + contract_title + notice_title; `observed_at` = award_date, else published; `document_verified`; `extra.contract` = buyer, value, currency, quantity, framework, tenders received, TED URL… |
| `suppliers.catalogue->'items'[i]` | `catalogue` / supply | `catalogue:<supplier_id>/<md5(lower(product) \| brand_model \| cpv)>` | `suppliers.catalogue` / `<supplier_id>/<i>`; `source_index` = i | items without `product` are skipped; text = product + brand_model + specs as "key: value" lines (sorted by key); `verification_status` from `evidence` (unknown value → `declared`); `observed_at` = `track_record.last_awarded`; `extra.catalogue` = category, confidence, commercial, unit, evidence, track_record |
| `ted_notices` with `kind = 'competition'` | `ted_notice` / demand | `ted:<publication_number>` | `ted_notices` / publication_number | `buyer_id` when a buyer matches (rule below), else null; text = title_en + title_original + description; `extra.notice` = deadlines, estimated value, buyer type… |
| `ted_awards` row (one per winner) | `ted_notice` / supply | `ted:<publication_number>/<winner_key>` | `ted_awards` / `<publication_number>/<winner_key>` | `supplier_id` when the winner matches a supplier (rule below); text from the joined notice; created once the notice row exists; `extra.award` = winner data, notice value |

Company matching for suppliers (`product_bridge_match_supplier`, revised 2026-10-30 in migrations
`product_bridge_supplier_matching` and `…_subsidiaries`):
- **The names must agree.** `product_bridge_name_key` is the name without legal form (GmbH, S.p.A., Sp. z o.o., OÜ …), accents and punctuation. Two keys agree when they are equal, or when one starts with the other and both are at least 8 characters long.
- **An identifier match alone is not enough.** TED sometimes gives a winner another company's identifier: Vortex Eesti carried Baltic Fox's, ZM Tarnów carried Łucznik's, SODARCA carried Antero Lopes'. These rows are no longer linked.
- **Cross-border links need an exact match.** Equal keys may link across countries when the identifier also matches or the name has at least 12 characters. A prefix match ("Rheinmetall Waffe Munition" vs "… Schweiz AG") must be in the same country, because a foreign subsidiary is a different legal entity.
- **Result `[VERIFIED 2026-10-30]`:** 121 award items linked to 17 suppliers. Before the revision it was 107 items and 19 suppliers, including 5 wrong links.

Buyers (`product_bridge_match_buyer`): the digits of the identifier first (`'DE 811144119'` → `811144119`), else equal
normalised `legal_name` or `name`. When both countries are known they must be equal.

After the backfill `[VERIFIED 2026-10-30]`:
- 372 contract items + 339 catalogue items + 2,230 demand items + 1,036 award items = 3,977, equal to the source rows.
- 107 award items are linked to 19 of the 20 suppliers; 450 demand items are linked to the 5 buyers.
- TED numbers grow with every hourly sync.

Not in the bridge: `supplier_due_diligence`, `supplier_kyc*`, `supplier_deal_baseline`, `supplier_documents`. They hold
company facts, not products.

### 3.2 Safety rules of the bridge
- A trigger never makes your write fail. Errors are caught and logged with `raise warning`; the row is skipped.
  - Consequence: **a broken bridge is silent.** Your sync succeeds, but the items stop updating. That is why §4 and §6 exist.
- Upsert by `(source_kind, source_ref)`. `source_text` is only rewritten when it differs, so `text_hash` stays and the
  classification is kept. `extra` is merged: the bridge owns its own top-level key (`contract`, `catalogue`, `notice`, `award`).
- A DELETE in your table does not delete the item. `supabase_sync.py` deletes and re-inserts each supplier's contracts on
  every run. The re-insert hits the same `source_ref`, so nothing is lost and nothing is reclassified.
- Orphans (the source row is gone and `source_seen_at` is older than 1 hour) are removed by
  `product_bridge_cleanup_orphans()`. The cron job `product-bridge-cleanup` runs it at :47 every hour, together with
  `product_bridge_relink()`, which re-matches award winners to suppliers and notices to buyers.
- Exception: the catalogue is synced as a whole. When `suppliers.catalogue` changes, catalogue items whose ref has
  disappeared are deleted at once, not after an hour.
- The `suppliers` update trigger fires only when `catalogue`, `name`, `legal_name`, `identifier` or `country` change.
  A profile upsert that changes none of them costs nothing.
- `product_bridge_backfill()` re-reads every source table and returns counts per source plus an `errors` count. Run it
  after any change to the bridge.
- The `product_bridge_*` functions are not exposed through PostgREST (service role only). Run them in the SQL editor.

### 3.3 Classifier
Migration `20261030140649_product_classifier_rules`.
- The cron job `product-classify` runs `product_classify_pending(500)` every 10 minutes. It works through the queue view
  `product_items_pending` and returns `{processed, by_outcome, errors, remaining, seconds}`.
  `product_classify_item(id)` does one item.
- Stages 0–2 of [26 §3](26-product-classification-pipeline.md) run in SQL: noise rules → CPV → rules (keywords, calibre,
  CPV, catalogue category). `extractor_version = 'rules-v1'`.
- It writes `class_id`, `family_id`, `attrs` (calibre, plus catalogue `specs` keys that map to the dictionary),
  `item_classifications` (cpvsuppl tags such as FB02, DA44), and `extra.detected_calibres`, `extra.unmapped_specs`,
  `extra.rules_scores`.
- Undecided items keep their class, get `family_id = null`, `needs_review = true` and a confidence below 0.5.
  An LLM pass decides them later (`classification_method = 'llm'`, `extractor_version = 'llm-agent-v1'`).
- An item that makes the classifier fail is marked processed with `needs_review = true` and `extractor_version = 'rules-v1-error'`, so it cannot block the queue.
- Decisions by an LLM, an agent or a person are kept while the item's text is unchanged.
- Every family and every attribute value is also a row in `claims` (`subject = 'item:<uuid>'`, predicate `HAS_FAMILY`
  or `attr:<key>`, `source_kind = 'rules'`). The claim's evidence level follows the source: `document_verified` for TED,
  the catalogue's own status for catalogue items.
- First rules run `[VERIFIED 2026-10-30]`: all 3,977 items processed. 1,187 have a family (420 by CPV, 767 by rules);
  2,790 are undecided (`needs_review`) and wait for the LLM pass. By source: contracts 84 of 372, catalogue 177 of 339,
  awards 316 of 1,036, demand notices 610 of 2,230. Many TED titles name no product ([25 §2](25-product-families.md)),
  so a low rules share is expected. Until the LLM pass runs, the UI shows chips only for decided items.
- `product_classify_calibres(text)` finds calibres in free text (known codes first). It is the only classifier function
  open to `anon`, so the browser can call it through `/rest/v1/rpc/product_classify_calibres` instead of a regex of its own.

### 3.4 UI
- `web/config.js` sets `window.SalesPatriotConfig` = `{SUPABASE_URL, SUPABASE_ANON_KEY}` (the publishable key). The key
  is public by design; RLS makes it read-only.
- `web/ontology.js` exposes `window.Ontology`. It reads `product_family_tree`, `product_calibres` and the `ui_*` objects over
  PostgREST (`/rest/v1/<view>?select=…`, `/rest/v1/rpc/<function>`).
  - Main calls: `load`, `loadTenderFamilies`, `loadSupplierItems`, `familiesForSupplier`, `familiesForTender`,
    `itemForCatalogue`, `itemForContract`, `matchTender`, `regulatory`, `family`, `calibre`, `calibreLabel`, `onChange`.
  - Every call fails soft (8 s timeout, HTTP error or empty answer → empty cache). Answers are cached for 5 minutes,
    failures retried after 30 s. `onChange` listeners redraw when new data arrives.
  - Add `?no-ontology` to any URL to switch the layer off and compare with the static-only UI.
- Strictly additive: if Supabase does not answer or returns nothing, the UI renders exactly as before.
  `calibreKeys`, `isWeapons` and `sameFamily` in `agents.js` are now the fallback path only: they decide when there are
  no ontology rows, and they help pick a catalogue item that is not classified yet.
- `agents.js`: `bestForNotice`, `rankForNotice` and `opportunitiesFor` take an optional `onto` argument. Ontology matches
  (`ui_match_tender` rows) come first; local matches the ontology did not rank are kept after them.
- `ui_match_tender` itself has fallbacks while a tender has no family yet: `class` (the item has a class only) and `cpv`
  (the item is not classified; class from the CPV prefix). A tender classified out of scope returns no rows.
- The UI still loads company profiles from the static `/data/profiles/*.json`. It joins ontology rows to them with
  `supplier_id`, `buyer_id`, `publication_number` and, for catalogue items, `source_index`.

## 4. Rules of engagement
### (a) Adding, renaming or dropping a column that the bridge reads
Columns the bridge reads, from the function bodies in migration `20261030134613_product_bridge` `[VERIFIED 2026-10-30]`:

| Table | Columns |
|---|---|
| `supplier_contracts` | almost all: id, supplier_id, notice_number, lot_id, title, description, contract_title, notice_title, cpv_codes, award_date, published, and into `extra.contract` buyer, buyer_country, notice_type, contract_signed, quantity, duration, framework_agreement, awarded_value, awarded_currency, tenders_received, winner_confirmed, ted_url. Not read: co_winners, subcontracting |
| `suppliers` | id, name, legal_name, identifier, country, catalogue |
| `buyers` | id, name, legal_name, identifier, country |
| `ted_notices` (not yours) | publication_number, kind, notice_type, procedure_type, publication_date, title_en, title_original, description, original_language, buyer_*, main_activity, cpv_codes, focus_classes, estimated_value/currency, deadline_tender, deadline_request, place_of_performance, ted_url |
| `ted_awards` (not yours) | publication_number, winner_key, winner_name, winner_identifier, winner_country, winner_size, winner_owner_nationality, decision_date, notice_value, currency, co_winners |

The classifier and the UI API read a few of your columns too `[VERIFIED 2026-10-30]`:

| Reader | Your columns |
|---|---|
| Classifier (`product_classify_apply`) | `suppliers.catalogue->'items'[source_index]`: `category`, `specs` |
| `ui_supplier_families`, `ui_match_tender` | `suppliers.id`, `name`, `legal_name` (via `ui_name_key`) |
| `ui_match_tender` | `supplier_kyc.kyc_status`, `kyc_checked_at` (a "KYC …" reason); `supplier_contracts.supplier_id`, `notice_number`, `buyer` ("already delivered to this buyer") |

Two traps:
- Dropping `catalogue`, `name`, `legal_name`, `identifier` or `country` from `suppliers` fails: the trigger
  `product_bridge_suppliers_upd` depends on them. **Never use `drop column … cascade` there**; it silently drops the trigger.
- Renaming any column above, or dropping one of the others, succeeds. The bridge then fails on every row, with only a
  warning in the log.

Checklist:
- [ ] Find the functions and views that use the column:
  ```sql
  select proname from pg_proc
  where (proname like 'product_bridge_%' or proname like 'product_classify_%' or proname like 'ui_%')
    and prosrc ilike '%<column>%';
  select viewname from pg_views where viewname like 'ui_%' and definition ilike '%<column>%';
  ```
- [ ] Update those functions **in the same migration** as the column change (`create or replace function`). Keep the
  safety pattern of §3.2. A broken `ui_` function errors at call time, and the UI then quietly falls back to the old logic.
- [ ] Apply it, then run `select product_bridge_backfill();`.
- [ ] Check the counts (§6.1): items per `source_table` equal the source rows.
- [ ] Check that the queue drains within 10–20 minutes (§6.2).
- [ ] Don't skip the checks. A rename without the function update does not break your sync; it silently freezes the items.

Adding a column that the bridge does not read needs nothing.

### (b) Changing the catalogue JSON shape
The bridge (`product_bridge_sync_catalogue`, `product_bridge_catalogue_ref`, `product_bridge_catalogue_text`) reads
`suppliers.catalogue->'items'` (an array). Per item it reads `product` (required), `brand_model`, `cpv` (one code as a
string), `specs` (a flat key → value object), `category`, `confidence`, `commercial`, `unit`, `evidence`
(incl. `verification_status`) and `track_record.last_awarded`.
- [ ] Moving `items`, renaming one of these keys or turning `cpv` into a list: update those functions in the same change,
  run the backfill, check the counts. Same checklist as (a). Nothing fails loudly: a list in `cpv` becomes a garbage code,
  a missing `items` array means zero catalogue items.
- [ ] Know what changes an item's identity. `source_ref` hashes `lower(product)`, `brand_model` and `cpv`:
  - editing one of them creates a new item (classified again), and the bridge deletes the old one in the same sync. That is expected;
  - editing `specs`, `category`, `evidence` or `commercial` only updates the existing item.
- [ ] Reordering items only changes `source_index`. The UI uses `source_index` to put a chip on the right catalogue card.
  `web/ontology.js` also checks that the item title equals `product`: with a stale index it looks the item up by title,
  or shows no chip, never a wrong one. Still, publish the JSON and run `supabase_sync.py` together.
- [ ] Keep `evidence.verification_status` in {`declared`, `document_verified`, `inferred`}. Any other value is silently
  stored as `declared`.
- [ ] New `specs` keys are fine. The classifier maps the obvious ones to the attribute dictionary and keeps the rest in
  `extra.unmapped_specs`. Don't align keys by hand in the JSON; the dictionary does that.
- [ ] `category` values are a classifier hint. These strings are mapped to a class or to candidate families: `Ammunition`,
  `Firearms`, `Parts of firearms and ammunition`, `Miscellaneous weapons`, `Optics and sights`, `Protective equipment`,
  `Services`, `Explosives and energetics`, `Less-lethal and police equipment` (`Other` and `Systems and integration` give no
  hint). Renaming one or adding a new one silently drops the hint. Update the category `case` in `product_classify_apply`
  in the same change, reviewed by the classifier side, then reclassify the catalogue items (§6.2).

### (c) Adding a new source table
Examples: platform offers or demands, a buyer catalogue, uploaded historical deals.
- [ ] Pick `source_kind` from the allowed values: `ted_lot`, `ted_notice`, `offer`, `demand`, `declared`, `upload`,
  `catalogue`. A new value needs a reviewed change of the check constraint on `product_items`.
- [ ] Define a **stable** `source_ref` from natural keys. Never use an identity id that changes on re-insert
  (`supplier_contracts.id` changes on every sync).
- [ ] Copy the pattern of the existing four sources in migration `20261030134613_product_bridge`:
  - a `product_bridge_upsert_<source>(key)` function that builds the row and calls the generic
    `product_bridge_upsert_item(…)` (it does the upsert on `(source_kind, source_ref)`, sets `source_seen_at`, keeps the text hash);
  - a `product_bridge_trg_<table>()` trigger function (`security definer`, `set search_path = ''`) whose body is wrapped in
    `begin … exception when others then raise warning …; end;`;
  - an `after insert or update … for each row` trigger named `product_bridge_<table>`; no DELETE trigger;
  - `source_text` built deterministically (same input → same text, e.g. with `product_bridge_join_text`), so `text_hash`
    only changes when the content changes;
  - `revoke all … from public, anon, authenticated` and `grant execute … to service_role` on the new functions.
- [ ] Add the table to `product_bridge_backfill()` and to `product_bridge_cleanup_orphans()`.
- [ ] Add a row to §3.1 here and to [26 §2](26-product-classification-pipeline.md).
- [ ] Run the backfill and check the counts (§6.1).
- [ ] Company facts (checks, documents, scores) do not belong in the bridge.

### (d) New product types, keywords, attributes, calibres or families
Never edit the taxonomy rows (`product_families`, `product_family_keywords`, `product_calibres`, `product_value_aliases`,
`product_family_crosswalk`, …) or the classifier code by hand. The next seed overwrites a hand edit, and without a version
bump nothing is reclassified.

Two paths:
1. **Proposal** (no code; people and agents both use it):
   ```sql
   insert into product_taxonomy_proposals (kind, target_family_id, payload, support_count, example_item_ids, created_by)
   values ('new_alias', 'ammunition.small_calibre',
           '{"attribute": "calibre", "alias": "9mm para", "canonical": "9x19"}', 3,
           array['<item uuid>']::uuid[], '<your name>');
   ```
   `kind` is one of `new_family`, `new_attribute`, `new_enum_value`, `new_alias`, `new_keyword`, `split_family`,
   `merge_families`, `move_family`. `status` starts as `open`.
2. **Reviewed PR**, after a person approves the proposal:
   - [ ] Edit `data/families.yaml` (families, keywords, attributes, `calibres` with `aliases`, crosswalk).
   - [ ] Bump `taxonomy_version` (1 → 2) and `released`.
   - [ ] `python3 seed_product_taxonomy.py --check`, then `python3 seed_product_taxonomy.py > seed.sql` and run `seed.sql` in Supabase.
   - [ ] Set the proposal's `status` to `applied`.
   - [ ] The queue view re-queues every item with an older `taxonomy_version`; the classifier reclassifies them on its next runs. Check §6.2.
   - [ ] Never rename a family id. Deprecate it and add a new one ([25 §3](25-product-families.md)).

### (e) Showing something new in the UI
- [ ] Read it from a `ui_*` view or function through `window.Ontology`.
- [ ] Need a new shape? Add a new `ui_` view in a migration: `security_invoker = true`, only rows with `visibility = 'public'`,
  `grant select` to `anon, authenticated`.
- [ ] Never re-implement classification, family rules or calibre normalisation in JavaScript.
  `calibreKeys`, `isWeapons`, `sameFamily` and `CATEGORY_FAMILY` in `agents.js` are fallback only: keep them working
  offline, but do not extend them. Need a calibre from free text (e.g. a typed demand)? Call
  `/rest/v1/rpc/product_classify_calibres` with `{"p_text": …}`.
- [ ] Keep it additive: every ontology element must disappear cleanly when `window.Ontology` returns nothing.
- [ ] Label the evidence. Show the method and the review state (`rules`, `llm`, `needs_review`), and show unverified
  crosswalk rows as unverified. A family that an LLM read from an official notice is still `inferred`
  ([26 §2](26-product-classification-pipeline.md)).

### (f) Agent conversations in `agents.js`
State on 2026-10-30: `buildDeal` shows the matched family and calibre as evidence in the conversation, but still anchors
the price on the catalogue's `commercial.derived_unit_price`. The list below is the target.
- [ ] Ground each conversation on three things:
  - the tender's family and attributes (`ui_tender_families`);
  - the supplier's proof in that family (`ui_supplier_families`: proven lots vs declared items);
  - the match reasons from `ui_match_tender`.
- [ ] Take price anchors only from the **same family**, and the same calibre when the family has one. Never anchor
  9x19 ammunition on a 9x19 pistol contract. Query in §6.5.
- [ ] Say what the anchor is: an awarded lot value, a unit price derived from a stated quantity, or a catalogue
  `commercial` note. Fictitious quantities and mandates stay labelled, as today.
- [ ] Regulatory lines (Military List, licence, ADR) come from `ui_regulatory_profile(family, calibre)`. They are defaults,
  never a licence decision. The per-deal checks D2/D3/D8 decide ([22](22-kyc-and-deal-due-diligence.md)).

## 5. What not to do
| Don't | Why | Do instead |
|---|---|---|
| Write family, calibre or regulatory fields into `suppliers.json`, `suppliers.catalogue` or new columns on your tables | A second copy goes stale and disagrees with `product_items` | Join on `supplier_id`; read `ui_supplier_families` |
| Normalise calibres or classify by CPV prefix in JavaScript or Python | Two normalisers give two answers | Read `ui_items.calibre`; for free text call `product_classify_calibres()`; in SQL `product_normalise_calibre()` |
| Edit taxonomy rows or keyword regexes by hand | Overwritten by the next seed; nothing is reclassified | §4d |
| Add a trigger, constraint or foreign key on your tables that can raise during the sync | One bad row stops the whole sync | Catch errors and `raise warning` (§3.2) |
| Insert or delete `product_items` from `supabase_sync.py` | Two writers for one source; idempotency breaks | Let the bridge and the orphan cleanup do it |
| Key anything on `supplier_contracts.id` | Identity values change on every sync | Natural key `supplier_id / notice_number / lot_id` |
| Present a `needs_review` or `llm` family as a fact | Evidence rules of the [README](README.md) | Show the method label |
| Change `ted_notices`, `ted_awards` or `ted-sync` | Owned by the TED sync session | Ask there |

## 6. How to verify after a change
Run these in the Supabase SQL editor (or with the MCP `execute_sql`).

### 6.1 Items per source
```sql
select 'supplier_contracts' as source, (select count(*) from supplier_contracts) as source_rows,
       (select count(*) from product_items where source_table = 'supplier_contracts') as items
union all
select 'suppliers.catalogue',
       (select coalesce(sum(jsonb_array_length(catalogue->'items')), 0) from suppliers
         where jsonb_typeof(catalogue->'items') = 'array'),
       (select count(*) from product_items where source_table = 'suppliers.catalogue')
union all
select 'ted_notices', (select count(*) from ted_notices where kind = 'competition'),
       (select count(*) from product_items where source_table = 'ted_notices')
union all
select 'ted_awards', (select count(*) from ted_awards),
       (select count(*) from product_items where source_table = 'ted_awards');
```
`source_rows` and `items` should be equal. Two catalogue items of one supplier with the same product, brand_model and
cpv collapse into one item (none on 2026-10-30).

```sql
select source_table, side, count(*) as items, count(family_id) as with_family,
       count(*) filter (where needs_review) as needs_review, max(source_seen_at) as last_seen
from product_items
group by source_table, side
order by source_table;
```

### 6.2 Queue and cron jobs
```sql
select count(*) from product_items_pending;          -- falls to about 0 within 10–20 minutes

select j.jobname, d.status, d.start_time, left(d.return_message, 120) as message
from cron.job_run_details d join cron.job j using (jobid)
where j.jobname like 'product%'
order by d.start_time desc
limit 10;

select product_classify_pending(500);                -- run one batch now instead of waiting for cron
```
The queue only holds items whose text changed or whose taxonomy version is old. After a change that does not touch
the text (e.g. a new catalogue `category` hint in the classifier), reclassify the affected items explicitly:
```sql
do $$
declare r record;
begin
  for r in select id from product_items where source_table = 'suppliers.catalogue' loop
    perform product_classify_item(r.id);
  end loop;
end $$;
```

### 6.3 Undecided items
```sql
select source_table, class_id, count(*)
from product_items
where family_id is null
group by 1, 2
order by 3 desc;

select id, source_table, title, cpv_codes, classification_method, confidence
from product_items
where needs_review
order by updated_at desc
limit 20;
```

### 6.4 One supplier's families
```sql
select * from ui_supplier_families where supplier_id = 'supplier-cze-65414128';

-- the same from the raw table
select family_id, attrs->>'calibre' as calibre, source_table, count(*)
from product_items
where supplier_id = 'supplier-cze-65414128'
group by 1, 2, 3
order by 4 desc;
```

### 6.5 One tender: families, matches, regulatory profile, price anchors
```sql
select * from ui_tender_families where publication_number = '750233-2026';
select * from ui_match_tender('750233-2026', 10);
select ui_regulatory_profile('ammunition.small_calibre', '9x19');

-- same-family TED price anchors: awarded lot values of the 20 profiled suppliers
-- (the bridge copies the contract's value, currency and quantity into extra.contract)
select supplier_id, publication_number, lot_id, attrs->>'calibre' as calibre,
       extra->'contract'->>'awarded_value' as awarded_value,
       extra->'contract'->>'awarded_currency' as currency,
       extra->'contract'->>'quantity' as quantity,
       observed_at::date as awarded
from product_items
where source_table = 'supplier_contracts'
  and family_id = 'ammunition.small_calibre'
  and attrs->>'calibre' = '9x19'
order by observed_at desc nulls last;
```
Award items (`source_table = 'ted_awards'`) carry `extra.award.notice_value`. That is the notice total, often over
several lots and winners, so it is not a lot price. Use it only as an upper bound.

### 6.6 Bridge errors and orphans
- Bridge warnings go to the Postgres log: Supabase dashboard → Logs → Postgres, search `product_bridge`.
- Re-run the bridge by hand: `select product_bridge_backfill();` returns
  `{"supplier_contracts": n, "suppliers.catalogue": n, "ted_notices": n, "ted_awards": n, "errors": n}`. `errors` must be 0.
- Orphan candidates (contracts), removed by the hourly cleanup:
  ```sql
  select count(*) from product_items i
  where i.source_table = 'supplier_contracts'
    and not exists (select 1 from supplier_contracts c
                    where c.supplier_id || '/' || c.notice_number || '/' || coalesce(nullif(btrim(c.lot_id), ''), 'notice')
                          = i.source_pk);
  ```
- Company links: `select source_table, count(*), count(supplier_id), count(buyer_id) from product_items group by 1;`
  On 2026-10-30: 107 of 1,036 award items had a `supplier_id`, 450 of 2,230 demand items a `buyer_id`.

## 7. Object map
Names per merge contract 2026-10-30, reconciled with the database (`information_schema`, `pg_trigger`, `cron.job`) and
the repo on 2026-10-30. Status: **live** = deployed and checked; **planned** = not deployed when checked.
Area = who changes it: *taxonomy* (reviewed PR only, §4d), *bridge*, *classifier*, *UI API*, *front end*.

**Product items and taxonomy** (before the merge)

| Object | Kind | Area | Purpose | Status |
|---|---|---|---|---|
| `product_items` | table | bridge, classifier | One row per item, both sides; links `supplier_id`, `buyer_id`, `source_table`, `source_pk`, `source_index`, `source_seen_at` (migration `20261030133533_product_items_source_links`) | live |
| `claims` | table | classifier | Evidence per family and attribute value | live |
| `product_taxonomy_proposals` | table | anyone (insert) | Change queue for the taxonomy | live |
| `product_taxonomy_versions`, `product_families`, `product_attribute_defs`, `product_family_attributes`, `product_family_crosswalk`, `product_family_keywords`, `product_calibres`, `product_value_aliases`, `product_noise_rules`, `product_cpvsuppl_tags` | tables | taxonomy | Seeded from `data/families.yaml` | live |
| `product_family_tree`, `product_items_pending` | views | taxonomy | Family list; classifier work queue | live |
| `product_family_attribute_list`, `product_family_schema`, `product_family_candidates_by_cpv`, `product_normalise_calibre`, `product_match_supply` | functions | taxonomy | Schemas, CPV stage, calibre normaliser, supply matching | live |

**Bridge** (migration `20261030134613_product_bridge`)

| Object | Kind | Purpose | Status |
|---|---|---|---|
| `product_bridge_supplier_contracts` on `supplier_contracts` | trigger, after insert or update | Contract → `ted_lot` supply item | live |
| `product_bridge_suppliers_ins` / `product_bridge_suppliers_upd` on `suppliers` | triggers, after insert / after update when catalogue, name, legal_name, identifier or country change | Catalogue → items; org name; relink | live |
| `product_bridge_ted_notices` on `ted_notices` | trigger, after insert or update | Competition notice → demand item; refreshes its award items | live |
| `product_bridge_ted_awards` on `ted_awards` | trigger, after insert or update | Award winner → supply item | live |
| `product_bridge_trg_supplier_contracts`, `_trg_suppliers`, `_trg_ted_notices`, `_trg_ted_awards` | trigger functions (security definer, never raise) | Call the upserts inside an exception block | live |
| `product_bridge_upsert_item` | function | Generic upsert on `(source_kind, source_ref)`; keeps the text hash; merges `extra` | live |
| `product_bridge_upsert_contract(id)`, `product_bridge_sync_catalogue(supplier_id)`, `product_bridge_upsert_notice(pub)`, `product_bridge_upsert_award(pub, winner_key)`, `product_bridge_refresh_awards(pub)` | functions | One per source | live |
| `product_bridge_match_supplier`, `product_bridge_match_buyer`, `product_bridge_relink()` | functions | Company matching (suppliers: names must agree, identifier supports; buyers: identifier digits, else name; see §3.1) | live |
| `product_bridge_name_key`, `product_bridge_names_compatible` | functions | Name key without legal form, accents, punctuation; equal or prefix (≥ 8 chars) comparison | live |
| `product_bridge_backfill()` → jsonb | function | Re-reads all four sources; counts + errors | live (run once on 2026-10-30: 3,977 items) |
| `product_bridge_cleanup_orphans()` → int | function | Deletes items whose source row is gone for > 1 hour | live |
| `product_bridge_digits`, `_norm_name`, `_try_date`, `_join_text`, `_catalogue_ref`, `_catalogue_text` | helper functions | Shared by the above | live |
| `product-bridge-cleanup` | pg_cron job, `47 * * * *` | Cleanup + relink, 30 min after `ted-sync-hourly` | live |

All `product_bridge_*` functions: execute for `service_role` only, not callable through PostgREST.

**Classifier** (migration `20261030140649_product_classifier_rules`)

| Object | Kind | Purpose | Status |
|---|---|---|---|
| `product_classify_apply(id uuid)` | function (internal) | The cascade for one item; returns the outcome | live |
| `product_classify_item(id uuid)` | function | Wrapper for one item | live |
| `product_classify_pending(p_limit int default 500)` → jsonb | function | Works through `product_items_pending`; returns processed, by_outcome, errors, remaining, seconds | live |
| `product_classify_calibres(p_text text)` | function, open to `anon` | Calibres found in free text, known codes first | live (checked as `anon`) |
| `product-classify` | pg_cron job, `*/10 * * * *` | Runs `product_classify_pending(500)` | live |
| LLM pass (`classification_method = 'llm'`, `extractor_version = 'llm-agent-v1'`) | process | Decides items left with `family_id = null` | planned |

`product_classify_apply`, `_item` and `_pending`: no execute for `anon` / `authenticated` (service role and cron only).

**UI data API** (migrations `20261030134817_ui_data_api`, `20261030135241_ui_data_api_refine`,
`20261030135639_ui_data_api_company_key`, `20261030135932_ui_data_api_match_speed`; security invoker, public rows only,
granted to `anon`, `authenticated`)

| Object | Kind | Purpose | Status |
|---|---|---|---|
| `ui_items` | view | Items with class and family names, `calibre`; extras: source_kind, source_ref, observed_at, quantity, unit, cpv_codes | live |
| `ui_supplier_families` | view | Per supplier and family: proven_lots, declared_items, calibres, last_award, best_evidence; extras: in_focus, item_count. Also counts unlinked award items whose winner has the supplier's name key | live |
| `ui_tender_families` | view | Per tender and family: calibres, confidence, method, deadline, is_open; extras: class_name, in_focus | live |
| `ui_match_tender(p_publication_number, p_limit)` | function | Companies for a tender, one row each (by `supplier_id`, else a supplier with the same name key, else the name key), best first, with reasons (incl. exact vs related calibre, "already delivered to this buyer") | live |
| `ui_regulatory_profile(p_family_id, p_calibre)` → jsonb | function | Crosswalk per scheme + derived item rules + notes; the calibre's own STANAG first | live (checked with a call) |
| `ui_name_key(p_name)` | function | Company name key (lower case, no punctuation, trailing legal form such as GmbH, a.s., Sp. z o.o. dropped); used to group companies and to attach awards the bridge did not link | live |

**Front end**

| File | Purpose | Status |
|---|---|---|
| `web/config.js` | `window.SalesPatriotConfig` (URL, publishable key) | in the working tree, not committed |
| `web/ontology.js` | `window.Ontology`: PostgREST reads, caches, fail-soft | in the working tree, not committed |
| `web/agents.js`, `web/app.js`, `web/index.html`, `web/styles.css` | Additive edits: family chips, calibre and regulatory badges, ontology-first matching with local fallback | in the working tree, not committed |

## 8. FAQ
**My sync deleted the contracts. Did the classification disappear?**
No. The DELETE does not touch `product_items`. The re-insert of the same contract hits the same `source_ref`, the text
hash is unchanged, so the family, attributes and claims stay. Only contracts that do not come back are removed, by the
orphan cleanup, an hour later.

**I added a new supplier profile. What do I need to do?**
Nothing beyond the usual pipeline. `supabase_sync.py` inserts the supplier, its contracts and catalogue; the triggers
create the items; the classifier picks them up within 10 minutes. Check with §6.4.

**Do I need to change `supabase_sync.py`?**
No. It keeps its DELETE + re-INSERT pattern. The bridge was built around it.

**How do I add a calibre, or a new spelling of one?**
- A spelling of a known calibre (e.g. "9mm Para"): proposal `new_alias`.
- A new calibre (e.g. 6.8x51): proposal `new_enum_value` with `{"attribute": "calibre", "code": "6.8x51", …}`.
- Then the reviewed PR of §4d: add it under `calibres:` in `data/families.yaml` (code, label, kind, bore, case,
  default family, aliases, STANAG), bump the version, seed.
- Until then `product_normalise_calibre()` still returns a pattern value such as `6.8x51`, so equal spellings match.
  Never add a calibre to `calibreKeys` in `agents.js`.

**Where do the regulatory badges come from?**
From `ui_regulatory_profile(family, calibre)`. It returns the family's rows of `product_family_crosswalk` per scheme
(`eu_ml`, `fsc`, `hs`, `adr`, `fd_2021_555`, `stanag`; each with `verified`, a note and a source URL), plus `derived`
values from the item rules of [25 §7](25-product-families.md) and general `notes`.
- Example `[VERIFIED 2026-10-30]`: `ui_regulatory_profile('firearms.machine_guns', '12.7x99')` gives `derived.ml_by_calibre`
  = ML1.a (bore 12.7 mm ≤ 12.7), `derived.fsc_by_bore` = 1005, `calibre_standard` = STANAG 4383, HS 9301 90 00,
  Firearms Directive A2.
- The badges are defaults for the D2/D3/D8 checks, not a licence decision.
- Rows with `verified = false` (our inference, `[I]` in 25) must look different from verified ones.

**A catalogue item shows no family. Why?**
One of three reasons:
- it is still in the queue (§6.2);
- the rules could not decide (`needs_review = true`, waiting for the LLM pass);
- it is out of scope (class `out_of_scope`): protective gear, optronics, services and so on. That is correct, not a bug.

**An award won by my supplier has no `supplier_id`. Why?**
The bridge links a TED winner to a supplier only when the names agree, after legal form, accents and punctuation are
removed. An identifier match alone is not enough, and foreign subsidiaries are not linked (§3.1). If the profile name
differs from what TED publishes, the award item keeps only `org_ref` (the winner name). Fix the profile and re-sync: a change of `identifier`, `legal_name`,
`name` or `country` triggers `product_bridge_relink()`, which also runs hourly. Never patch `product_items` by hand.
Note: `ui_supplier_families` and `ui_match_tender` still count such an award for the supplier when the winner name has
the same `ui_name_key` (legal form ignored), so the UI may show it even though `supplier_id` is null.

**The UI shows no chips or badges.**
That is the fallback working: Supabase did not answer, `web/config.js` has no key, the URL has `?no-ontology`, or the
items are not classified yet. Check the browser's network tab (`/rest/v1/ui_…` requests) and §6.1–§6.2. The rest of the
page must look exactly as before.

**Can the UI write to the ontology?**
No. The anon key is read-only. Agent listings (offers, demands) will go through a validated insert (backlog T8 in
[26 §8](26-product-classification-pipeline.md)), not through PostgREST writes from the browser.

**What do "proven lots" and "declared items" mean?**
- Proven lots = TED contracts and award wins in that family (`document_verified`).
- Declared items = catalogue items in that family (evidence as stated in the catalogue).
Show proven before declared.

**How fast does a change show up?**
- The trigger runs in the same transaction as your write.
- The classifier runs every 10 minutes.
- TED data arrives hourly (sync at :17). A new TED notice is classified within about 10 minutes after the sync writes
  it, so within about 70 minutes of it appearing in the TED API.

## 9. Open points
- Competition notices are notice-level items. Lot-level demand items need the eForms XML (backlog T2 in [26 §8](26-product-classification-pipeline.md)).
- `org_id` on `product_items` is unused. It waits for one canonical company id ([10](10-ontology-data-model.md)).
- Only 5 buyers are profiled, so most demand items have `buyer_id = null` (1,780 of 2,230 on 2026-10-30). That is expected;
  `org_ref` keeps the buyer name.
- 17 of the 20 suppliers have at least one linked award item (121 items). Award items for winners outside the 20
  profiles stay unlinked by design. Kept unlinked on purpose: foreign Rheinmetall subsidiaries (ARGES, Schweiz) and
  "Baltic Fox OU" with country CZE.
- Two company-matching rules exist: the bridge (`product_bridge_name_key`: names must agree, identifier is supporting
  evidence, prefix matches only in the same country) and the UI API (`ui_name_key`: legal form dropped, no country
  check). They can disagree. Merge them into one rule when the canonical company id of [10](10-ontology-data-model.md)
  arrives.
