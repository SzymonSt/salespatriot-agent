# Agent context API + RAG + memory: implementation guide

Goal: buyer and supplier agents (Google ADK + Gemini in `app/backend`) get **one fast, uniform way** to
read everything we built: TED data, the product ontology, company and KYC facts, past deals (RAG) and
learned lessons (memory). Every conversation and outcome they produce flows back into the RAG and
memory, so the system improves with every deal.

Written 2026-10-30 from what is live in Supabase project `hackathon` (`aodxjmjnsqvadxcrepfr`) and in the
repo. It is meant to be split across several agents; §3 is the contract they all code against.

---

## 0. What exists today (do not rebuild)

| Layer | Objects (live) | Owner, change only via them |
|---|---|---|
| TED | `ted_notices` (4,840), `ted_awards` (1,036), view `ted_open_tenders` (`next_deadline`, `deadline_kind`), Edge Function `ted-sync`, cron `ted-sync-hourly` (:17) | TED session (KB 25-supabase-ted-sync) |
| Companies | `suppliers`, `buyers`, `supplier_contracts`, `supplier_due_diligence`, `supplier_kyc`, `supplier_kyc_checks`, `supplier_deal_baseline`, `supplier_documents`, view `supplier_overview` | profiles / KYC (`supabase_sync.py`, `supplier_due_diligence.py`) |
| Product ontology | `product_families` (40), attribute defs, crosswalks, calibres, `product_items` (3,977), `claims`, `product_taxonomy_proposals`; functions `product_family_schema`, `product_match_supply`, `product_normalise_calibre` | ontology session (KB 25-product-families, 26, 27) |
| Bridge | triggers `product_bridge_*` copy suppliers, contracts, catalogue, TED rows into `product_items`; cron `product-bridge-cleanup` (:47) | ontology session |
| Classifier | `product_classify_pending()` + cron every 10 min: **planned** (today `family_id` is null on all items) | ontology session |
| UI data API | views `ui_items`, `ui_supplier_families`, `ui_tender_families`; functions `ui_match_tender(pub, limit)`, `ui_regulatory_profile(family, calibre)` | ontology / UI |
| Backend | FastAPI + ADK; `run_structured_agent()`; supplier preview and buyer draft routes; synthetic `/api/demo/run`; DB only `SELECT 1` | backend (docs/agent-workflow.md) |
| Web UI | `web/` reads PostgREST with the publishable key through `window.Ontology` | UI |
| Deal checks | `deal_due_diligence.py` + `deal_rules.py`: D1–D14 per open tender × supplier → GO / GO WITH CONDITIONS / ESCALATE / NO-GO; `--push` writes Supabase `deal_checks`, `deal_check_items`; mandates from `agent_mandates` (tables not yet in the database on 2026-10-30) | due-diligence owner |

**Missing (this guide):** a RAG store, an embedding pipeline, one context call for agents, persisted
agent conversations, memory, and the learning loop.

## 1. Decisions

1. **Agents read through one call: `agent_context`.** It is a Postgres function (RPC) that assembles a
   compact JSON "context pack" from the objects above, plus RAG hits and memory. The backend exposes it
   as `POST /v1/context`, and ADK agents get it as a tool. Agents never query tables directly.
2. **Logic lives in SQL; the backend is a thin layer.** RPCs reuse `ui_*` and `product_*`. The backend
   adds the query embedding, auth, logging and the ADK tools. The UI keeps its own `ui_*` reads.
3. **Agents talk through persisted messages.** `agent_conversations` + `agent_messages`. The backend runs
   the next agent turn when a message arrives (in process for the demo). The UI shows the timeline via
   Supabase Realtime. Today's synthetic demo becomes a persisted run.
4. **RAG store = `kb_chunks`** (pgvector + full text) in the same database. Chunks are rebuilt
   deterministically from source rows. Embeddings are added asynchronously by the Edge Function `kb-embed`.
5. **Embeddings = Gemini `gemini-embedding-001`, 768 dimensions.** We already hold a Google key. The model
   is multilingual (TED text is in 24 languages), and 768 keeps HNSW fast. Verify the model name and
   parameters in the Gemini API docs before coding. Vectors with reduced dimensions must be L2-normalised.
6. **Memory = `agent_memory`.** Episodes are written automatically after each conversation. Lessons are
   distilled later and used platform-wide only once validated.
7. **Naming and ownership.** New objects use the prefixes `kb_` (RAG), `agent_` (conversations, memory,
   context) and `deal_`. Never alter `ted_*`, `product_*`, `ui_*` or the supplier tables. Read them only.
8. **Three knowledge layers. History is frozen; only our own data teaches the memory.**
   | Layer | Source | Changes | Visibility |
   |---|---|---|---|
   | `baseline` | One-off harvest of several thousand historical TED notices and what happened to them (WS8) | **Never updated.** Extending it means a new snapshot (`baseline_v2`) built next to `v1`; `v1` stays for comparison and backtests | public |
   | `ted_live` | Hourly `ted-sync`, only notices published **after** the snapshot cutoff | append and upsert | public |
   | `platform` | Our own conversations, quotes, outcomes, references, uploaded deals, episodes, lessons | append only; the **only** layer that writes `agent_memory` | parties (validated lessons public) |
   Agents always see which layer a fact comes from. Baseline numbers are priors; platform numbers refine them (§3.4).

## 2. Architecture

```
            ted-sync (:17)        supabase_sync.py        bridge + classifier (every 10 min)
                 │                        │                          │
   ted_notices · ted_awards     suppliers · buyers · contracts    product_items · claims · product_families
                 └──────────────┬─────────┴──────────────────────────┘
                                ▼
          kb_refresh() (cron :52, plus after each conversation)  ──►  kb_chunks (content, refs, family, calibre,
                                                                      cpv, country, date, visibility, fts, embedding)
                                                       kb-embed Edge Function (cron */5) fills embedding ◄──┘
                                ▼
   agent_context(role, purpose, subject, query_embedding, viewer)   ◄── kb_search() + ui_* + memory_recall()
                                ▼
   FastAPI  POST /v1/context  ·  ADK tool get_context  ·  POST /v1/conversations/... (messages)
                                ▼
   buyer agent ⇄ agent_messages ⇄ supplier agent   ──► conversation closed ──► agent_memory (episode)
                                                                         └──► kb_chunks (deal card)
   lesson distiller (cron) ──► agent_memory (lesson, proposed → validated)
   backtest (recall@5 on TED awards) ──► kb_metrics
```

## 3. Contract (code against this; change only by agreement)

### 3.1 Embedding
- Model `gemini-embedding-001`, `output_dimensionality = 768`, L2-normalised.
- `task_type = RETRIEVAL_DOCUMENT` for chunks and `RETRIEVAL_QUERY` for queries.
- Column type `extensions.vector(768)`. Store `embedding_model = 'gemini-embedding-001@768'`.

### 3.2 `kb_chunks`
| Column | Type | Notes |
|---|---|---|
| `id` | uuid pk | |
| `kind` | text | `tender`, `award`, `contract`, `supplier_profile`, `buyer_profile`, `catalogue_item`, `conversation`, `kb_doc`, `pattern` |
| `source_key` | text, unique with `kind`, `layer`, `snapshot_id` | Stable natural key, e.g. `ted:749708-2026`, `award:720852-2026/cenzin sp. z o.o.`, `contract:<supplier_id>/<notice>/<lot>`, `supplier:<id>`, `conv:<uuid>`, `kb:23-self-learning-rag#how-accuracy-grows` |
| `ref` | text | What agents cite. Same value as `source_key`, or the TED URL form for notices |
| `title` | text | One line |
| `content` | text | Deterministic template (§5.3), ≤ 2,000 chars |
| `facts` | jsonb | Structured fields the agent may need without reading `content` |
| `publication_number`, `supplier_id`, `buyer_id`, `item_ids uuid[]` | | Links to facts |
| `class_id`, `family_id`, `calibre`, `cpv_codes text[]`, `country char(3)`, `occurred_at date` | | Filters |
| `layer` | text | `baseline`, `ted_live` or `platform` (decision 8) |
| `snapshot_id` | text null | e.g. `baseline_v1`; set only for `layer = 'baseline'` |
| `visibility` | text | `public` or `parties` |
| `party_ids text[]` | | Supplier and buyer ids allowed to see `parties` rows |
| `content_hash` | text | `md5(content)`. When it changes, `embedding` is set to null |
| `embedding` | vector(768) null | Filled by `kb-embed` |
| `embedding_model`, `embedded_at` | | |
| `fts` | tsvector generated | `to_tsvector('simple', title || ' ' || content)` |
| `updated_at` | timestamptz | |

### 3.3 RPC signatures (all `security definer`, `set search_path = ''`, execute granted to `service_role` only)
```sql
kb_search(p_query_embedding extensions.vector(768) default null, p_query_text text default null,
          p_kinds text[] default null, p_family_id text default null, p_calibre text default null,
          p_cpv_prefix text default null, p_country text default null, p_viewer text default null,
          p_limit int default 8) returns jsonb          -- [{ref, kind, title, snippet, facts, score}]

memory_recall(p_query_embedding extensions.vector(768) default null, p_role text default null,
              p_family_id text default null, p_country text default null, p_counterparty text default null,
              p_viewer text default null, p_limit int default 5) returns jsonb

memory_write(p_entry jsonb) returns uuid                 -- validates kind/status/visibility
agent_context(p_role text, p_purpose text, p_subject jsonb,
              p_query_embedding extensions.vector(768) default null, p_query_text text default null,
              p_viewer text default null, p_limit int default 8) returns jsonb
kb_refresh(p_kinds text[] default null, p_since timestamptz default null) returns jsonb   -- counts
kb_embed_queue(p_limit int default 100) returns setof kb_chunks                            -- for kb-embed
kb_embed_store(p_rows jsonb) returns int                                                    -- [{id, embedding, model}]
```
`p_viewer` = the calling agent's company id (`suppliers.id` or `buyers.id`). Rows with
`visibility = 'parties'` are returned only when `p_viewer = any(party_ids)`.

### 3.4 `agent_context` purposes and the context pack
| `p_purpose` | `p_subject` | Used by | Main sources |
|---|---|---|---|
| `assess_tender` | `{publication_number, supplier_id}` | supplier agent | `ui_tender_families`, `ui_regulatory_profile`, `ui_supplier_families` (own proof), `kb_search` awards/contracts same family + calibre, price anchors, buyer history, memory |
| `find_suppliers` | `{publication_number}` or `{family_id, calibre, country}` | buyer agent | `ui_match_tender`, `supplier_overview` + `supplier_due_diligence` per candidate, `kb_search`, memory |
| `evaluate_quote` | `{conversation_id, message_id}` | buyer agent | quote payload, price anchors same family/calibre, `supplier_deal_baseline`, KYC/DD, regulatory |
| `check_counterparty` | `{supplier_id}` or `{buyer_id}` | both | `supplier_overview`, `supplier_kyc_checks`, `supplier_due_diligence`, contracts, peer references (later) |
| `search` | `{}` + `p_query_text` | both | `kb_search` only |

The pack has the same shape for every purpose. Sections that do not apply are empty, never missing:
```json
{
  "purpose": "assess_tender",
  "subject": {"publication_number": "750233-2026", "supplier_id": "supplier-cze-65414128"},
  "facts": {
    "tender": {"ref": "ted:750233-2026", "title": "…", "buyer": "…", "country": "BGR",
               "families": [{"family_id": "ammunition.small_calibre", "calibre": ["9x19"], "method": "rules", "confidence": 0.8}],
               "next_deadline": "2026-11-27T21:59:59Z", "deadline_kind": "tenders",
               "estimated_value": 325000, "currency": "EUR", "value_source": "procedure"},
    "self": {"supplier_id": "…", "proven_lots": 3, "declared_items": 5, "best_evidence": "document_verified", "kyc_score": 7.1}
  },
  "candidates": [],
  "evidence": [{"ref": "contract:supplier-cze-…/…/LOT-1", "kind": "contract", "title": "…", "snippet": "…", "score": 0.82}],
  "price_anchors": [{"ref": "…", "family_id": "…", "calibre": "9x19", "value": 1290000, "currency": "CZK",
                     "quantity": "…", "basis": "awarded_lot_value"}],
  "regulatory": {"eu_ml": "ML3", "adr": "UN0012 1.4S", "verified": false, "note": "defaults, not a licence decision"},
  "memory": [{"ref": "memory:…", "kind": "lesson", "text": "…", "support": 4, "status": "validated"}],
  "warnings": ["family assigned by rules (confidence 0.8)", "estimated value unknown for 2 of 5 anchors"],
  "refs": ["ted:750233-2026", "…"],
  "versions": {"taxonomy": 1, "kb": "2026-10-30T13:52Z", "context": "v1"}
}
```
Rules: ≤ `p_limit` items per list, snippets ≤ 300 chars, and everything carries a `ref`. Agents must cite
refs in `agent_messages.refs`. Unknown means unknown: never zero, never a guess.

Every `evidence` and `price_anchors` item also carries `"layer"`. Statistics come in one extra section,
`priors`, that keeps the layers apart and shows the blend:
```json
"priors": {
  "scope": {"family_id": "ammunition.small_calibre", "calibre": "9x19", "country": "POL"},
  "baseline": {"snapshot_id": "baseline_v1", "n": 41, "unit_price_p25": 1.21, "unit_price_p50": 1.42,
               "unit_price_p75": 1.66, "currency": "PLN", "bids_median": 3, "value_published_share": 0.52},
  "platform": {"n": 5, "unit_price_vs_baseline_p50": -0.06},
  "blend": {"weight_platform": 0.33, "unit_price_p50": 1.37, "rule": "w = n_platform / (n_platform + 10)"}
}
```
The baseline is read from the **active** snapshot only (`kb_snapshots.active = true`).

### 3.5 Conversations and memory tables
```text
agent_conversations(id uuid pk, publication_number text, buyer_id text, supplier_id text,
  family_id text, status text check in ('open','quoted','accepted','rejected','escalated','closed'),
  buyer_mandate jsonb, supplier_mandate jsonb, demo boolean default true,
  outcome jsonb, created_at, closed_at)
agent_messages(id uuid pk, conversation_id uuid fk, seq int, sender_role text check in ('buyer','supplier','system'),
  sender_id text, type text check in ('introduction','rfq','quote','counter','accept','reject','question',
  'answer','reference_request','reference_answer','escalate','check'), text text, payload jsonb,
  refs text[], model text, agent_version text, created_at)   unique (conversation_id, seq)
agent_tool_calls(id bigint pk, conversation_id uuid, message_id uuid, tool text, args jsonb,
  result_refs text[], latency_ms int, created_at)
deal_outcomes(conversation_id uuid pk, delivered_on_time boolean, quality text, final_price numeric,
  currency text, notes text, reported_by text, created_at)
agent_memory(id uuid pk, kind text check in ('episode','lesson','preference','pattern_proposal'),
  role text, family_id text, country char(3), counterparty text, text text, facts jsonb,
  evidence_refs text[], support int default 1, status text check in ('proposed','validated','rejected','superseded'),
  visibility text check in ('public','parties','owner'), owner_id text, party_ids text[],
  content_hash text, embedding vector(768), embedding_model text, use_count int default 0,
  helpful int default 0, unhelpful int default 0, expires_at timestamptz, created_at, updated_at)
```
`agent_memory` holds **platform-layer knowledge only**. Lessons may cite baseline refs as context, but they are
created only from our own conversations and outcomes. Historical patterns live in `kb_patterns` (WS8), not here.

### 3.6 Backend HTTP (FastAPI, local and unauthenticated like the existing routes; add auth before exposing)
| Method and path | Body | Calls |
|---|---|---|
| `POST /v1/context` | `{role, purpose, subject, query?, viewer_id}` | embed the query → `agent_context` |
| `POST /v1/knowledge/search` | `{query, kinds?, family_id?, calibre?, cpv_prefix?, country?, viewer_id, limit?}` | `kb_search` |
| `POST /v1/conversations` | `{publication_number, buyer_id, supplier_id, buyer_mandate, supplier_mandate, demo}` | insert, then run the first supplier turn |
| `POST /v1/conversations/{id}/messages` | `{sender_role, sender_id, type, text?, payload?, refs?}` | validate, insert, run the counterpart's turn |
| `GET /v1/conversations/{id}` | — | conversation + messages |
| `POST /v1/conversations/{id}/outcome` | `deal_outcomes` fields | insert; write episode memory; refresh the conversation chunk |
| `POST /v1/memory/recall` | `{query, role, family_id?, country?, counterparty?, viewer_id}` | `memory_recall` |
| `POST /v1/memory` | memory entry | `memory_write` (agents may only write `episode` / `preference` / `pattern_proposal` as `proposed`) |
| `POST /v1/memory/{id}/feedback` | `{helpful: bool}` | counters |

ADK tools (same functions, in process): `get_context`, `search_knowledge`, `recall_memory`,
`get_family_schema` (wraps `product_family_schema(family, 'listing')`), `regulatory_profile`.

---

## 4. Work split (parallel agents)

| WS | Owner agent | Depends on | Delivers |
|---|---|---|---|
| WS1 RAG store | DB agent A | — | `vector` extension, `kb_chunks`, `kb_refresh()` builders, `kb_search()`, cron `kb-refresh` |
| WS2 Embeddings | Edge agent | WS1 table (columns from §3.2 are enough to start) | Edge Function `kb-embed`, `kb_embed_queue/store`, cron `kb-embed` */5, backfill |
| WS3 Context RPC | DB agent B | §3 contract; WS1 for real hits (stub `kb_search` returning `[]` until then) | `agent_context()` with all 5 purposes |
| WS4 Conversations + memory | DB agent C | — | tables in §3.5, `memory_recall/write`, RLS, Realtime publication |
| WS5 Backend | Backend agent | §3 contract (mock the RPCs until WS3/WS4 land) | `knowledge/` module, ADK tools, HTTP routes, persisted demo run, tests |
| WS6 Learning loop | ML agent | WS4, WS5 | episode writer, lesson distiller, feedback, backtest recall@5, `kb_metrics` |
| WS7 UI (optional) | UI agent | WS4, WS5 | conversation timeline via Realtime, evidence chips from `refs` |
| WS8 Baseline snapshot | Data agent | WS1 table (adds `layer`, `snapshot_id`); WS2 for embeddings | frozen `baseline_v1`: harvest, tender ↔ outcome linkage, baseline items, stats, patterns, chunks, file in Storage, immutability |

Coordination rules for every agent:
- Read [§0](#0-what-exists-today-do-not-rebuild) and §3 first. Apply migrations with the Supabase MCP `apply_migration` (project `aodxjmjnsqvadxcrepfr`)
  and save the same SQL under `supabase/migrations/<timestamp>_<name>.sql`.
- Never modify `ted_*`, `product_*`, `ui_*`, `supplier*`, `buyers` or their triggers. Read only.
- Every new table: RLS on. Public data gets an anon `select` policy. Private or platform data gets no anon policy.
  Write through RPCs or the service role.
- Every new function: `security definer`, `set search_path = ''`, fully qualified names, explicit grants.
- After each migration, run `get_advisors(security)` and fix new findings.
- No secrets in git. Edge Function secrets are set via `create_edge_function_secret` (the user types the value).

---

## 5. Step by step

### WS1: RAG store (DB agent A)

**1.1 Extension and table.** Migration `kb_store`:
```sql
create extension if not exists vector with schema extensions;

create table public.kb_chunks (
  id uuid primary key default gen_random_uuid(),
  kind text not null check (kind in ('tender','award','contract','supplier_profile','buyer_profile',
                                     'catalogue_item','conversation','kb_doc','pattern')),
  source_key text not null,
  ref text not null,
  title text not null,
  content text not null,
  facts jsonb not null default '{}',
  publication_number text, supplier_id text, buyer_id text, item_ids uuid[] not null default '{}',
  class_id text, family_id text, calibre text, cpv_codes text[] not null default '{}',
  country char(3), occurred_at date,
  layer text not null check (layer in ('baseline','ted_live','platform')),
  snapshot_id text,
  check ((layer = 'baseline') = (snapshot_id is not null)),
  visibility text not null default 'public' check (visibility in ('public','parties')),
  party_ids text[] not null default '{}',
  content_hash text generated always as (md5(content)) stored,
  embedded_hash text,                           -- content_hash at the time of embedding
  embedding extensions.vector(768),
  embedding_model text, embedded_at timestamptz,
  fts tsvector generated always as (to_tsvector('simple', coalesce(title,'') || ' ' || content)) stored,
  updated_at timestamptz not null default now(),
  unique nulls not distinct (kind, source_key, layer, snapshot_id)
);
create index kb_chunks_embedding on public.kb_chunks using hnsw (embedding extensions.vector_cosine_ops);
create index kb_chunks_fts on public.kb_chunks using gin (fts);
create index kb_chunks_family on public.kb_chunks (family_id, calibre);
create index kb_chunks_cpv on public.kb_chunks using gin (cpv_codes);
create index kb_chunks_pending on public.kb_chunks (updated_at)
  where embedding is null or embedded_hash is distinct from content_hash;
alter table public.kb_chunks enable row level security;
create policy "public read" on public.kb_chunks for select to anon, authenticated using (visibility = 'public');
```
Acceptance: the table exists, `get_advisors(security)` shows no new warnings.

**1.2 Chunk builders.** One SQL function per kind, each returning rows in `kb_chunks` shape, plus
`kb_refresh(p_kinds, p_since)` that upserts on `(kind, source_key, layer, snapshot_id)`.
`kb_refresh` writes only `layer = 'ted_live'` (TED rows with `publication_date` after the active snapshot's
`cutoff_date`; before WS8 lands, all TED rows) and `layer = 'platform'` (profiles, catalogue, conversations).
It **never** touches `layer = 'baseline'`; WS8 writes those once. Set `updated_at = now()` only when
`content` or `facts` changed (compare `md5(content)`), so unchanged rows are not re-embedded.

Content templates. Keep them deterministic: same input → same text, no `now()`.
| Kind | Source | `content` template (English labels, original-language text kept) | Key `facts` |
|---|---|---|---|
| `tender` | `ted_notices` kind=competition ⨝ `product_items` (source_table=`ted_notices`) | `TENDER {pub} · {buyer_country} · {buyer_name} · {procedure_type} · legal basis {legal_basis}\nWhat: {family name or class name} {calibre?}\nTitle: {title_en}\nOriginal: {title_original}\n{description ≤1200}\nDeadline: {coalesce(deadline_tender, deadline_request)} ({kind})\nEstimated value: {value currency or 'not published'}` | pub, buyer, country, family_id, calibre, value, currency, deadlines, is_open |
| `award` | `ted_awards` ⨝ `ted_notices` ⨝ items (`ted_awards`) | `AWARD {pub} · {buyer_country} · buyer {buyer_name} → winner {winner_name} ({winner_country}, {winner_size})\nWhat: {family} {calibre}\n{title_en}\nNotice value: {notice_value currency} (notice total, {co_winners} winner(s))\nDecided: {decision_date}` | pub, buyer, winner, winner_identifier, family_id, calibre, notice_value, co_winners |
| `contract` | `supplier_contracts` ⨝ items (`supplier_contracts`) | `CONTRACT {supplier name} won lot {lot_id} of {notice_number} · buyer {buyer} ({buyer_country})\nWhat: {family} {calibre}\n{title}\n{description ≤800}\nAwarded: {awarded_value currency} · quantity {quantity} · tenders received {n} · {award_date}` | supplier_id, notice, lot, family_id, calibre, awarded_value, currency, quantity, unit_price if derivable |
| `supplier_profile` | `suppliers` ⨝ `supplier_overview` ⨝ `ui_supplier_families` ⨝ `supplier_due_diligence` | `SUPPLIER {name} ({country}, {size}) id {id}\nProven families: {family: n lots, calibres}\nDeclared families: {…}\nDue diligence: {total}/10 ({verdict}, confidence {c})\nKYC: {status}` | supplier_id, families, dd_total, kyc_status |
| `buyer_profile` | `buyers` + their `ted_notices` | `BUYER {name} ({country}, {legal_type}) · {n} tenders since {year} · families {…} · typical procedures {…}` | buyer_id, families, counts |
| `catalogue_item` | `product_items` source_kind=catalogue | `CATALOGUE {supplier} · {family} {calibre}\n{title}\n{source_text ≤800}\nCommercial: {extra.catalogue.commercial}` | supplier_id, item_id, family_id, calibre |
| `conversation` | `agent_conversations` + messages + outcome (WS4) | `DEAL {status} · buyer {b} ↔ supplier {s} · {family} {calibre}\nAsked: …\nQuoted: …\nOutcome: …` | conversation_id, final_price, currency, outcome; `visibility='parties'`, `party_ids={buyer,supplier}` |
| `kb_doc` | repo `knowledge_base/*.md`, split on `##` | `KB {file} · {heading}\n{section text}` | file, heading |

`kb_doc` rows are loaded by a small script (WS5: `python -m app.backend.knowledge.load_kb_docs`) that upserts via the
service role, because the files live in the repo, not in the database.

Family and calibre come from `product_items` (`family_id`, `attrs->>'calibre'`). **Today they are null until
the classifier runs.** The builders must still work, with family set to `class_id`, or to the CPV class from
`cpv_codes`. Because family is part of `content`, the chunk is re-embedded automatically once classification lands.

**1.3 Schedule.** `cron.schedule('kb-refresh', '52 * * * *', $$select public.kb_refresh()$$)`: after ted-sync
(:17), the classifier and the bridge cleanup (:47). Also call `kb_refresh(array['conversation'])` from WS5
when a conversation closes.

**1.4 `kb_search`.** Hybrid search with reciprocal rank fusion:
```sql
-- inside kb_search: base = filtered + visible rows
with base as (
  select * from public.kb_chunks k
  where (p_kinds is null or k.kind = any(p_kinds))
    and (p_family_id is null or k.family_id = p_family_id or k.class_id = p_family_id)
    and (p_calibre is null or k.calibre is null or k.calibre = p_calibre)
    and (p_cpv_prefix is null or exists (select 1 from unnest(k.cpv_codes) c where c like p_cpv_prefix || '%'))
    and (p_country is null or k.country = p_country)
    and (k.visibility = 'public' or p_viewer = any(k.party_ids))
),
vec as (select id, row_number() over (order by embedding <=> p_query_embedding) r
        from base where p_query_embedding is not null and embedding is not null
        order by embedding <=> p_query_embedding limit 40),
txt as (select id, row_number() over (order by ts_rank_cd(fts, websearch_to_tsquery('simple', p_query_text)) desc) r
        from base where p_query_text is not null and fts @@ websearch_to_tsquery('simple', p_query_text) limit 40)
select jsonb_agg(… order by score desc) from (
  select b.ref, b.kind, b.title, left(b.content, 300) as snippet, b.facts,
         coalesce(1.0/(60+vec.r),0) + coalesce(1.0/(60+txt.r),0)
           + case when p_calibre is not null and b.calibre = p_calibre then 0.01 else 0 end as score
  from base b left join vec using (id) left join txt using (id)
  where vec.id is not null or txt.id is not null
     or (p_query_embedding is null and p_query_text is null)   -- filters only: newest first
  order by score desc, b.occurred_at desc nulls last limit p_limit) s;
```
Acceptance:
- `select kb_refresh();` returns counts per kind: tender ≈ 2,230, award ≈ 1,036, contract ≈ 372, supplier_profile 20, buyer_profile 5, catalogue_item ≈ 339.
- `kb_search(null, 'cartridges 9x19', array['award','contract'])` returns rows before embeddings exist (full-text path).

### WS2: Embeddings (Edge agent)

**2.1 RPCs** (migration `kb_embed_rpc`):
- `kb_embed_queue(p_limit)` returns rows where `embedding is null or embedded_hash is distinct from content_hash`, ordered by `updated_at`.
- `kb_embed_store(p_rows jsonb)` sets `embedding`, `embedding_model`, `embedded_hash = content_hash`, `embedded_at = now()`.
- Grant both to `service_role` only. Use the same pattern for `agent_memory` (WS4): `memory_embed_queue` / `memory_embed_store`.

**2.2 Edge Function `kb-embed`.** Copy the pattern of `supabase/functions/ted-sync`: Vault secret checked
through an RPC, `verify_jwt = false`, `GET` returns status.
- Secret `GOOGLE_API_KEY`: ask the user to set it with `create_edge_function_secret`. Never paste the key.
- Per run: take up to 100 rows from `kb_embed_queue`, call Gemini batch embedding (`gemini-embedding-001`,
  `RETRIEVAL_DOCUMENT`, 768 dims; check the current REST shape in the Gemini docs), L2-normalise, then
  `kb_embed_store`. Repeat while the queue is non-empty and less than 100 s have passed (Edge wall clock limit is 150 s).
- On HTTP 429: stop the run and leave the rest for the next one. Log to `kb_embed_runs` (mode, embedded, errors).

**2.3 Cron.** `kb-embed` every 5 minutes via `pg_net`, same trigger-function pattern as `ted_sync_trigger`.

**2.4 Backfill.** Trigger it until the queue is empty (~4,000 chunks ≈ 40 batches). Check the Gemini quota first
(docs/local-development.md, quotas section).

Acceptance:
- `select count(*) from kb_chunks where embedding is null` reaches 0.
- A vector query for "155 mm artillery shells Spain" returns Spanish 155 mm tenders and awards in the top 5.

### WS3: Context RPC (DB agent B)

**3.1** Migration `agent_context`. Implement `agent_context(...)` as plpgsql that builds the pack of §3.4:
- `assess_tender`:
  - `facts.tender` from `ted_notices` + `ui_tender_families`;
  - `facts.self` from `ui_supplier_families` (where `supplier_id`) + `supplier_overview`;
  - `evidence` = `kb_search(kinds=['award','contract','tender'], family/calibre of the tender)`;
  - `price_anchors` = `product_items` with `source_table='supplier_contracts'`, same family (and calibre), values from `extra->'contract'` (query of KB 27 §6.5);
  - `regulatory` = `ui_regulatory_profile(family, calibre)`;
  - `memory` = `memory_recall(role='supplier', family, buyer_country, counterparty=buyer)`.
- `find_suppliers`: `candidates` = `ui_match_tender(pub, p_limit)`, each joined with `supplier_overview` (KYC) and
  `supplier_due_diligence` (total, verdict); `evidence` = `kb_search(kinds=['contract','award'], family)`; `memory` for role buyer.
- `evaluate_quote`: load the message `payload`; `price_anchors` as above; flag a quote outside p25–p75 of the
  anchors as a warning; `facts.counterparty` = KYC + DD; `regulatory`; `facts.deal_checks` = the latest
  `deal_checks` / `deal_check_items` row for this tender × supplier (do not re-implement D1–D14).
- `check_counterparty`: KYC, DD categories, recent contracts (`kb_search` kinds `contract` with `supplier_id`).
- `search`: `evidence` = `kb_search(p_query_embedding, p_query_text, …)` only.
- `priors` (§3.4) for the subject's family, calibre and country:
  - baseline row from `kb_baseline_stats` of the active snapshot, falling back from family+calibre+country
    → family+country → family → class;
  - platform numbers from closed conversations and `deal_outcomes` in the same scope;
  - blend weight `n_platform / (n_platform + 10)`.
- Always fill `warnings`:
  - family assigned by `rules` or `llm`, or `needs_review` → say so;
  - value not published → "unknown";
  - award `notice_value` used → "notice total, not a lot price".

**3.2 Tests** (SQL, in the migration notes):
- `select agent_context('supplier','assess_tender','{"publication_number":"750233-2026","supplier_id":"<a profiled supplier>"}')`
  returns all keys, ≤ 8 items per list, and every item has a `ref`.
- Same call for `find_suppliers` with an open tender from `ted_open_tenders`.
- Timing target: < 300 ms without embedding, < 600 ms with (check with `explain analyze`).

### WS4: Conversations and memory (DB agent C)

**4.1** Migration `agent_conversations`: tables of §3.5 plus indexes on `(conversation_id, seq)` and
`agent_memory (status, family_id)` and HNSW on `agent_memory.embedding`.
- RLS on all of them. For the demo, add an anon `select` policy on `agent_conversations` / `agent_messages` only
  `where demo = true` (join through the conversation), so the UI can show demo timelines. Real conversations get no anon policy.
- Add `agent_messages` to the `supabase_realtime` publication.

**4.2 Functions:**
- `memory_write(p_entry)`:
  - validates `kind`;
  - agents may write `episode`, `preference`, `pattern_proposal` with `status='proposed'`;
  - only `service_role` jobs set `validated`;
  - computes `content_hash`.
- `memory_recall(...)`:
  - candidates = `validated` lessons (`visibility='public'`) + `episode`/`preference` where the viewer is an owner or party;
  - score = vector similarity (if an embedding is given) × status weight (validated 1.0, proposed 0.5)
    × recency `exp(-age_days/180)` × `(helpful+1)/(helpful+unhelpful+2)`;
  - increments `use_count` of the returned rows;
  - returns ≤ `p_limit` items with `ref = 'memory:<id>'`.
- `agent_message_append(p_conversation_id, p_msg jsonb)`: assigns `seq` atomically and checks the state machine:
  - `rfq` only when `open`;
  - `quote`/`counter` only after an `rfq`;
  - `accept` only after a `quote`/`counter`;
  - `accept` sets status `accepted`; `escalate` sets `escalated`.
- `agent_conversation_close(p_id, p_outcome jsonb)`:
  - sets `closed_at` and `outcome`;
  - inserts an `episode` into `agent_memory` (text = compact summary of the messages, `evidence_refs` = union of message refs);
  - calls `kb_refresh(array['conversation'])`.

Acceptance:
- A scripted conversation (rfq → quote → counter → accept → close) gives 4 messages with `seq` 1..4,
  status `closed`, 1 episode row and 1 `kb_chunks` row of kind `conversation` with `visibility='parties'`.
- An illegal transition (`accept` before `quote`) raises.

### WS5: Backend integration (Backend agent; `app/backend`)

**5.1 Module `app/backend/knowledge/`:**
- `client.py` `KnowledgeClient(engine)`: async methods `context()`, `search()`, `recall()`, `remember()`,
  `append_message()`, `close_conversation()`. Each calls the RPC with SQLAlchemy `text("select public.agent_context(:role, …)")`.
  Requires `DATABASE_URL` with a role allowed to execute (service role or the `postgres` pooler user).
- `embeddings.py` `embed_query(text) -> list[float]`: google-genai embed call, `RETRIEVAL_QUERY`, 768, normalised.
  Cache by text (LRU) and time out after 3 s. On failure return None, and the RPC falls back to full text.
- `tools.py`: ADK function tools `get_context(purpose, subject, query)`, `search_knowledge(query, filters)`,
  `recall_memory(query, family_id)`, `get_family_schema(family_id)`, `regulatory_profile(family_id, calibre)`.
  The viewer id and role come from the agent factory (closure), never from the model.
- Pass `refs` through: tools return the pack, and the agent output schemas gain `refs: list[str]`.

**5.2 Agents** (`agents/main.py`): keep the existing instructions and safety rules. Add the tools to `build_supplier_agent`
and `build_buyer_agent`, plus one instruction line:
"Before answering, call get_context with the right purpose; cite refs; treat unknown values as unknown."
Keep the typed output models. ADK tool use + `output_schema` may conflict depending on the ADK version.
If it does, run the turn in two steps: the backend calls `agent_context` itself and puts the pack into the
payload; the agent only drafts. Document which path was used.

**5.3 Routes:** add the HTTP routes of §3.6 in a new router `app/backend/knowledge/routes.py`. Same error
conventions as `main.py` (`503` when `DATABASE_URL` is missing). Keep them bound to `127.0.0.1` until auth exists.

**5.4 Orchestrator** `conversations.py`: `start(publication_number, buyer_id, supplier_id, mandates)` loops:
1. supplier introduction;
2. buyer `find_suppliers` → buyer DD decision (existing policy gate) → `rfq`;
3. supplier `assess_tender` → `quote` (structured: unit price, currency, quantity, lead time, incoterms, conditions);
4. buyer `evaluate_quote` → `accept`, `counter` or `escalate`, within the mandate (max rounds, price ceiling,
   min evidence). Mandate checks run in Python, never in the model. Read mandates from `agent_mandates`
   when it exists (same source as `deal_due_diligence.py`), else the default policy;
5. on `accept`: `close_conversation` with the outcome.

Every step calls `append_message` and logs tool calls into `agent_tool_calls`.
Convert `/api/demo/run` to call this orchestrator with `demo=true`. Keep the synthetic fixtures only where real
data is missing, and label them in `payload.demo_fixture = true`.

**5.5 `load_kb_docs` script:** split `knowledge_base/*.md` on `##` and upsert `kb_doc` chunks (service role).

**5.6 Tests** (`app/backend/tests`):
- unit tests with a fake `KnowledgeClient`;
- one integration test marked `@pytest.mark.supabase`, skipped without `DATABASE_URL`, that calls `/v1/context` for a live open tender;
- orchestrator state-machine tests (no LLM: mock `run_structured_agent`).

Acceptance:
- `POST /v1/context` returns the pack in < 1 s locally.
- The demo run persists one conversation whose messages carry refs.
- `GET /v1/conversations/{id}` replays it.

### WS6: Learning loop (ML agent)

**6.1 Episode quality.** `agent_conversation_close` already writes the episode. Add the outcome fields
(price vs anchors, rounds, escalations) into `facts`.

**6.2 Lesson distiller** `python -m app.backend.knowledge.distill`, every 30 min or nightly:
1. group new episodes and `deal_outcomes` by (family, country, counterparty type);
2. Gemini proposes at most 3 lessons per group, as JSON: `text`, `support` = episode ids, `scope`;
3. insert them as `lesson` / `proposed` via `memory_write`;
4. auto-validate when support ≥ 3 and no contradicting outcome; otherwise leave them for human review.

Proposals about the taxonomy go to `product_taxonomy_proposals`, never into the ontology directly.

**6.3 Feedback.** After each agent turn, mark the memory refs the agent cited as used.
After the outcome: `helpful += 1` when the deal closed within the mandate, `unhelpful += 1` when it was escalated or rejected.

**6.4 Backtest** (`python -m app.backend.knowledge.backtest`). Always run it on the **same frozen snapshot**
(`baseline_v1`), so results stay comparable as platform data grows:
1. for every eForms-era award in `kb_baseline_procedures` (outcome `awarded`, winner linked), take its tender;
2. call `agent_context('buyer','find_suppliers', …)` restricted to data observed before the decision date
   (add `p_as_of date` to `ui_match_tender` via the ontology owner, or filter the candidates' proof by `observed_at` in the script);
3. record whether the real winner is in the top 5. Store recall@5 and MRR in `kb_metrics(run_at, metric, value, n, notes)`.

Show the trend in the demo only with real numbers.

**6.5 Ranking weights.** Log candidate features in `agent_tool_calls.args/result_refs`. Once there are ≥ 50 labelled
outcomes, fit a logistic regression offline. Store the weights in `kb_metrics` or a `ranking_models` table and pass
the version into `versions.context`.

Acceptance: `kb_metrics` holds a baseline recall@5; distilled lessons appear as `proposed` with support refs.

### WS8: Baseline snapshot, frozen historical knowledge (Data agent)

Goal: harvest several thousand historical TED procedures once, map **what happened** to each, turn them into
structured knowledge plus RAG chunks, and freeze the result as `baseline_v1`. New TED data goes to
`ted_live` and our own deals to `platform`. Neither ever edits the baseline.

**Scope of `baseline_v1`** (counts checked in the TED API on 2026-10-30):
| Part | Query (expert syntax) | Results |
|---|---|---|
| Core: whole weapons and ammunition group, every contract nature, all years | `classification-cpv IN (35300000)` with competition and result notice types | 3,330 results + their tenders |
| Buyer behaviour: defence directive, eForms era | `legal-basis=32009L0081 AND PD>=20231101` | 7,375 results + their tenders |
| Cutoff | `PD <= <cutoff_date>` (the harvest day) | everything after the cutoff belongs to `ted_live` |

Winners exist in the Search API only for eForms notices (from November 2023). Older results go in as
"outcome without winner" (value, date, procedure). Legacy-XML winners are a `baseline_v2` candidate.

**8.1 Tables** (migration `kb_baseline`; all public read, writes by service role):
```text
kb_snapshots(id text pk, cutoff_date date, status text check in ('building','frozen','retired'),
  active boolean default false, queries jsonb, counts jsonb, file_path text, file_sha256 text,
  taxonomy_version int, created_at, frozen_at)
  -- exactly one active snapshot: unique index on (active) where active
kb_baseline_notices(snapshot_id, publication_number, kind ('competition'|'result'|'modification'),
  procedure_id text, notice_type, publication_date, legal_basis, procedure_type, buyer_name, buyer_identifier,
  buyer_country, buyer_legal_type, main_activity, cpv_codes text[], title_en, description, estimated_value,
  estimated_currency, deadline, result_value, result_currency, bids_received numeric, raw jsonb,
  primary key (snapshot_id, publication_number))
kb_baseline_procedures(snapshot_id, procedure_id, tender_publication_number, result_publication_numbers text[],
  outcome text check in ('awarded','unsuccessful','cancelled','no_result_yet','result_only'),
  buyer_name, buyer_country, buyer_legal_type, class_id, family_id, calibre, cpv_codes text[],
  published date, decided date, days_to_award int, estimated_value, awarded_value, currency, bids_received,
  winners jsonb,          -- [{name, identifier, country, size, owner_nationality}]
  primary key (snapshot_id, procedure_id))
kb_baseline_stats(snapshot_id, scope_level text check in ('family_calibre_country','family_country','family','class'),
  class_id, family_id, calibre, country, buyer_legal_type,
  n_procedures int, n_awarded int, per_year jsonb, quantity_p50 numeric, unit_price_p25, unit_price_p50,
  unit_price_p75, currency, value_published_share numeric, bids_p50 numeric, top_winner_share numeric,
  top_winners jsonb, procedure_types jsonb, days_to_award_p50 int, repeat_interval_days_p50 int,
  primary key (snapshot_id, scope_level, coalesce(class_id,''), coalesce(family_id,''), coalesce(calibre,''),
               coalesce(country,''), coalesce(buyer_legal_type,'')))   -- use a unique index for the coalesces
kb_patterns(id text, snapshot_id, kind ('demand','supplier','qualification','collaboration','pricing'),
  scope jsonb, statement text, support int, examples text[], metrics jsonb, primary key (snapshot_id, id))
```

**8.2 Immutability** (same migration):
- a trigger `kb_baseline_frozen_guard` on `kb_baseline_notices`, `kb_baseline_procedures`, `kb_baseline_stats`,
  `kb_patterns` and on `kb_chunks` (rows with `layer = 'baseline'`). It raises on UPDATE or DELETE when the row's
  snapshot has `status = 'frozen'`;
- the embedding columns of baseline chunks are the only exception: `kb_embed_store` may set them while
  `embedding is null`;
- inserts into a frozen snapshot raise too. A new version is built as a new `snapshot_id` with `status = 'building'`.

**8.3 Harvest** (`app/backend/knowledge/baseline/harvest.py`, standard library + TED Search API):
- `paginationMode = ITERATION`, `limit = 250`, ≤ 40 fields (≤ 10,000 fields per page), 1 request per second.
- Fields: the 38 of `supabase/functions/ted-sync/sync.ts` plus `procedure-identifier` and `notice-identifier`.
- Run both scope queries for competition and result notice types (and `can-modif` for modifications),
  each with `PD<=<cutoff>`.
- Write raw notices to `kb-snapshots/baseline_v1/notices.jsonl.gz` in Supabase Storage (private bucket
  `kb-snapshots`), then bulk insert into `kb_baseline_notices` (normalised with the same helpers as `ted-sync`).
- Expect ~25,000 notices (tenders + results + modifications). Log counts per query into `kb_snapshots.counts`.

**8.4 Link tender ↔ outcome** (SQL function `kb_baseline_link(p_snapshot)`):
- Group notices by `procedure_id` (eForms `procedure-identifier`; checked live, e.g. `720852-2026` →
  `f1249740-…`). Older notices without it are grouped by buyer + title similarity + CPV within 18 months,
  or kept as `result_only`.
- Outcome:
  - `awarded`: a result with a winner or a value;
  - `unsuccessful` or `cancelled`: from the result-notice status fields;
  - `no_result_yet`: a tender without a result after its deadline + 12 months;
  - `result_only`: a result whose tender is outside the harvest.
- Fill `days_to_award`, winners and bids. For multi-lot eForms results, optionally fetch the XML (as
  `ted_supplier_contracts.py` does) to split value per lot. Cap this at the 353 group (~1,090 results) and run it politely.

**8.5 Products.** Classify each procedure with the ontology's read-only functions:
- `product_family_candidates_by_cpv(cpv_codes)`;
- `product_normalise_calibre(title || description)`;
- the rules stage of `product_classify_item` once it is live (call it on a temporary row, or reuse its SQL).

Store `class_id`, `family_id`, `calibre` and a `classification_method` in `kb_baseline_procedures`. **Do not
insert into `product_items`**: that table is the live ontology layer, owned by the ontology session.

**8.6 Stats and patterns** (`kb_baseline_build_stats(p_snapshot)`):
- `kb_baseline_stats` at the four scope levels.
- Unit price only when the quantity is stated (lot-level XML or text with an explicit quantity and unit).
  Otherwise leave it null; never divide a notice total by a guessed quantity.
- `kb_patterns` P001–P005 of KB 12, each with `support`, example procedure refs and the metric that proves it:
  - P001: buyers re-buying a family, with the interval;
  - P002: winner concentration per family and country;
  - P003: recurring qualification requirements (from description keywords);
  - P004: share of consortium wins;
  - P005: value bands.

**8.7 Chunks.** Insert baseline cards into `kb_chunks` with `layer = 'baseline'` and `snapshot_id = 'baseline_v1'`:
- one `tender` card per procedure (with the outcome line: "Outcome: awarded to X for Y after Z days, N bids");
- one `award` card per winner;
- one `pattern` card per pattern (add `pattern` to the `kind` check).

`kb-embed` (WS2) embeds them like any other chunk.

**8.8 Freeze and publish:**
- write `manifest.json` next to the JSONL in Storage: snapshot id, cutoff, queries, counts per table, `taxonomy_version`, sha256 of the file;
- set `kb_snapshots.status = 'frozen'`, `frozen_at = now()` and `active = true`;
- from then on, `kb_refresh` builds `ted_live` only for notices after `cutoff_date`.

**8.9 Extending later:** build `baseline_v2` (for example legacy-XML winners, more CPV groups or more years) as a
new snapshot next to `v1`. Compare backtests between the two, then switch `active`. Retire `v1` instead of deleting it.

Acceptance:
- `kb_snapshots` has one frozen, active `baseline_v1` with counts and a file hash.
- An UPDATE on any baseline row raises.
- `kb_baseline_procedures` links ≥ 80% of eForms-era results to their tender.
- `kb_baseline_stats` has rows for every in-focus family that has ≥ 5 procedures.
- `agent_context(... 'assess_tender' ...)` returns `priors.baseline` for a 9x19 tender.
- `kb_chunks` holds the baseline cards, all embedded.

### WS7: UI (optional, UI agent)
- Subscribe to `agent_messages` (Realtime, demo rows) and render the timeline: sender, type, text, ref chips
  that link to `ted_url` or open the chunk.
- The evidence panel for a tender reads `kb_chunks` (public) by `publication_number`, through `window.Ontology`
  or a new `ui_` view. Keep the UI additive and fail-soft, as in KB 27 §3.4.

---

## 6. Order and timing for a one-day build
1. **Hour 0:** everyone reads §0–§3. WS1, WS4, WS5 and the WS8 harvest (8.3) start in parallel; WS5 uses mocks.
2. **WS1 done:** WS2 starts the embedding backfill; WS3 switches from the stub to the real `kb_search`;
   WS8 loads its chunks after linking and stats, then freezes `baseline_v1`.
3. **WS3 + WS4 done:** WS5 swaps the mocks for the real RPCs; the demo persists conversations.
4. **WS6:** baseline backtest, then episodes and lessons once demo conversations exist.
5. **WS7** if time allows.

## 7. Definition of done
- [ ] `POST /v1/context` answers for every purpose, with refs, in < 1 s.
- [ ] Buyer and supplier agents use it as a tool (or with a pre-fetched pack) and cite refs in their messages.
- [ ] A demo conversation is persisted end to end, closed, and visible in the UI.
- [ ] Closing a conversation creates an episode memory and a `conversation` chunk; the next `agent_context` for the
      same family returns it to the parties.
- [ ] `kb_chunks` ≥ 4,000 rows, all embedded; `kb-refresh` and `kb-embed` crons are green.
- [ ] `baseline_v1` frozen and active: procedures linked to outcomes, stats, patterns, chunks, file + manifest in Storage.
      UPDATE and DELETE on baseline rows are blocked.
- [ ] `agent_context` shows `priors.baseline`, `priors.platform` and the blend, and every evidence item carries its `layer`.
- [ ] Baseline recall@5 stored in `kb_metrics`, computed on `baseline_v1`.
- [ ] Security advisors clean; no anon writes; private conversations invisible to anon.

## 8. Risks and mitigations
| Risk | Mitigation |
|---|---|
| Classifier not live → no families in chunks | Builders fall back to class / CPV class; chunks re-embed when family appears |
| Gemini embedding quota (free tier) | Batch 100, stop on 429, resume next run; check the AI Studio rate-limit page first |
| ADK tools + `output_schema` conflict | Two-step turn (backend pre-fetches the context pack) |
| Context too large for the model | `p_limit`, 300-char snippets, facts first; agents call `search_knowledge` for more |
| Price anchors misleading | Same family and calibre only; label the basis; award notice totals only as upper bounds |
| Memory poisoning | Only `validated` lessons are platform-wide; episodes stay with the parties; feedback counters decay bad entries |
| Two KB files numbered 25 | Rename one before merging (TED sync doc → 28) |
| History drifts into the memory | Baseline frozen by trigger; `agent_memory` only from the platform layer; extensions only as new snapshots |
| Unit prices invented from notice totals | Unit price only with an explicit quantity; otherwise null and a warning |
