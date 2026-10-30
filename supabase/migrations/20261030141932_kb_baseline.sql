-- WS8: frozen historical baseline snapshots (docs/agent-context-rag-implementation.md, decision 8 and §5 WS8 8.1-8.2).
-- A snapshot is a one-off TED harvest plus everything derived from it (procedures, stats, patterns, baseline
-- chunks). It is built with status 'building' and then frozen. Frozen (and retired) snapshots are immutable:
-- extending the baseline means a new snapshot_id (baseline_v2) built next to v1.
--
-- Public read (TED data is public). Writes only through the service role (Edge Function kb-baseline-harvest)
-- and the security-definer functions below.
--
-- One-off setup per project (not in git; the secret is generated inside the database):
--   select vault.create_secret('https://<project-ref>.supabase.co/functions/v1/kb-baseline-harvest', 'kb_baseline_url');
--   select vault.create_secret(replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''), 'kb_baseline_secret');

-- ---------------------------------------------------------------------------------------------------------
-- 8.1 Tables
-- ---------------------------------------------------------------------------------------------------------

create table if not exists public.kb_snapshots (
  id text primary key,
  cutoff_date date not null,                        -- PD <= cutoff; later notices belong to the ted_live layer
  status text not null default 'building' check (status in ('building', 'frozen', 'retired')),
  active boolean not null default false,            -- agent_context reads the active snapshot only
  queries jsonb not null default '[]',              -- [{key, kind, query}] TED expert queries of the harvest
  counts jsonb not null default '{}',               -- per query and per table, written at freeze
  manifest jsonb,                                   -- written at freeze: cutoff, queries, counts, taxonomy, hashes
  content_sha256 text,                              -- sha256 of the ordered list "publication_number:md5(raw)"
  file_path text,                                   -- optional JSONL export in Storage
  file_sha256 text,
  taxonomy_version int,
  harvest_state jsonb not null default '{}',        -- resumable cursor of kb-baseline-harvest
  notes text,
  created_at timestamptz not null default now(),
  frozen_at timestamptz,
  check (not active or status = 'frozen')
);
-- exactly one active snapshot
create unique index if not exists kb_snapshots_one_active on public.kb_snapshots (active) where active;

-- Raw harvested notices, normalised with the helpers of ted-sync (supabase/functions/kb-baseline-harvest/harvest.ts).
create table if not exists public.kb_baseline_notices (
  snapshot_id text not null references public.kb_snapshots (id),
  publication_number text not null,
  kind text not null check (kind in ('competition', 'result', 'modification')),
  queries text[] not null default '{}',             -- harvest query keys that returned this notice
  procedure_id text,                                -- eForms procedure-identifier (null for legacy notices)
  notice_identifier text,                           -- eForms notice-identifier
  notice_version int,
  notice_type text,
  publication_date date,
  legal_basis text,
  procedure_type text,
  contract_nature text[] not null default '{}',
  buyer_name text,
  buyer_identifier text,
  buyer_country text,
  buyer_legal_type text,
  main_activity text,
  cpv_codes text[] not null default '{}',
  title_en text,
  title_original text,
  description text,
  original_language text,
  estimated_value numeric,
  estimated_currency text,
  estimated_value_source text,                      -- procedure | lots | framework_max
  deadline timestamptz,                             -- tender deadline, else request-to-participate deadline
  deadline_kind text,                               -- tenders | requests_to_participate
  result_value numeric,                             -- result-value-notice, else legacy total-value
  result_currency text,
  bids_received numeric,
  winners jsonb not null default '[]',              -- [{name, identifier, country, size, owner_nationality}]
  decision_date date,
  place_of_performance text[] not null default '{}',
  direct_award_justification text,
  -- second, small field pass (status and lots)
  winner_selection_status text[] not null default '{}',   -- BT-142: selec-w | clos-nw | open-nw
  non_award_justification text[] not null default '{}',   -- BT-144: no-rece | all-rej | chan-need | ins-fund | ...
  previous_notice_ids text[] not null default '{}',       -- previous-notice-id-proc (tender publication number)
  modified_notice_ids text[] not null default '{}',       -- modification-previous-notice-identifier
  tendering_parties text[] not null default '{}',
  n_lots int,
  quantity numeric,                                 -- only when the notice has one lot with one stated quantity
  quantity_unit text,                               -- BT-625 unit code (H87 = piece)
  contract_conclusion_date date,
  extra_fetched boolean not null default false,
  raw jsonb not null,                               -- all fetched Search API fields (without "links")
  ted_url text generated always as ('https://ted.europa.eu/en/notice/-/detail/' || publication_number) stored,
  inserted_at timestamptz not null default now(),
  primary key (snapshot_id, publication_number)
);
create index if not exists kb_baseline_notices_procedure on public.kb_baseline_notices (snapshot_id, procedure_id);
create index if not exists kb_baseline_notices_kind on public.kb_baseline_notices (snapshot_id, kind, publication_date);

-- One row per procedure: tender <-> outcome (kb_baseline_link).
create table if not exists public.kb_baseline_procedures (
  snapshot_id text not null references public.kb_snapshots (id),
  procedure_id text not null,                       -- eForms procedure-identifier, else 'pub:<publication_number>'
  link_method text,                                 -- procedure_id | previous_notice | single_notice
  eforms boolean not null default false,
  tender_publication_number text,
  competition_publication_numbers text[] not null default '{}',
  result_publication_numbers text[] not null default '{}',
  modification_publication_numbers text[] not null default '{}',
  outcome text not null check (outcome in ('awarded', 'unsuccessful', 'cancelled', 'no_result_yet', 'result_only')),
  result_status text check (result_status in ('awarded', 'unsuccessful', 'cancelled', 'pending', 'unknown')),
  outcome_basis text,                               -- what decided the outcome (winner, value, status code, ...)
  overdue boolean,                                  -- no_result_yet and deadline (or publication) + 12 months < cutoff
  legal_basis text,
  procedure_type text,
  buyer_name text,
  buyer_identifier text,
  buyer_country text,
  buyer_legal_type text,
  main_activity text,
  title_en text,
  title_original text,
  description text,
  cpv_codes text[] not null default '{}',
  class_id text,
  family_id text,
  calibre text,
  classification_method text,                       -- cpv | rules | none (read-only port of the rules-v1 cascade)
  classification_confidence real,
  classification_detail jsonb,
  published date,
  deadline timestamptz,
  decided date,
  days_to_award int,
  estimated_value numeric,
  estimated_currency text,
  awarded_value numeric,
  currency text,
  n_lots int,
  quantity numeric,                                 -- explicit single-lot quantity (BT-25), else null
  quantity_unit text,
  unit_price numeric,                               -- awarded_value / quantity, only with an explicit quantity
  bids_received numeric,
  winners jsonb not null default '[]',              -- [{name, identifier, country, size, owner_nationality}]
  consortium boolean,
  non_award_reasons text[] not null default '{}',
  primary key (snapshot_id, procedure_id)
);
create index if not exists kb_baseline_procedures_family on public.kb_baseline_procedures (snapshot_id, family_id, calibre, buyer_country);
create index if not exists kb_baseline_procedures_tender on public.kb_baseline_procedures (snapshot_id, tender_publication_number);

-- Priors per scope (kb_baseline_build_stats). Read by agent_context (§3.4 priors).
create table if not exists public.kb_baseline_stats (
  id bigint generated always as identity primary key,
  snapshot_id text not null references public.kb_snapshots (id),
  scope_level text not null check (scope_level in ('family_calibre_country', 'family_country', 'family', 'class')),
  class_id text,
  family_id text,
  calibre text,
  country text,
  buyer_legal_type text,
  n_procedures int not null,
  n_awarded int not null,
  per_year jsonb,
  quantity_p50 numeric,
  quantity_unit text,
  n_unit_prices int not null default 0,
  unit_price_p25 numeric,
  unit_price_p50 numeric,
  unit_price_p75 numeric,
  currency text,                                    -- currency of the unit prices and of awarded_value_p50
  awarded_value_p50 numeric,
  value_published_share numeric,
  bids_p50 numeric,
  top_winner_share numeric,
  top_winners jsonb,
  procedure_types jsonb,
  days_to_award_p50 int,
  repeat_interval_days_p50 int,
  example_refs text[] not null default '{}'
);
create unique index if not exists kb_baseline_stats_scope on public.kb_baseline_stats (
  snapshot_id, scope_level, coalesce(class_id, ''), coalesce(family_id, ''), coalesce(calibre, ''),
  coalesce(country, ''), coalesce(buyer_legal_type, ''));

-- Historical patterns P001-P005 (KB 12). Platform lessons live in agent_memory, never here.
create table if not exists public.kb_patterns (
  snapshot_id text not null references public.kb_snapshots (id),
  id text not null,
  kind text not null check (kind in ('demand', 'supplier', 'qualification', 'collaboration', 'pricing')),
  scope jsonb not null default '{}',
  statement text not null,
  support int not null,
  examples text[] not null default '{}',
  metrics jsonb not null default '{}',
  primary key (snapshot_id, id)
);

-- ---------------------------------------------------------------------------------------------------------
-- 8.2 Immutability
-- ---------------------------------------------------------------------------------------------------------

-- Rows of a snapshot that is no longer 'building' cannot be inserted, updated or deleted. On kb_chunks only
-- rows with layer = 'baseline' are guarded, and updates that change nothing but the embedding columns
-- (kb_embed_store, WS2) are allowed.
create or replace function public.kb_baseline_frozen_guard()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  c_embedding constant text[] := array['embedding', 'embedding_model', 'embedded_hash', 'embedded_at'];
  v_old text;
  v_new text;
  v_locked text;
  v_exists boolean;
begin
  if tg_op = 'TRUNCATE' then
    execute format(
      'select exists (select 1 from %I.%I t join public.kb_snapshots s on s.id = t.snapshot_id
                      where s.status <> ''building'' %s)',
      tg_table_schema, tg_table_name,
      case when tg_table_name = 'kb_chunks' then 'and t.layer = ''baseline''' else '' end)
      into v_exists;
    if v_exists then
      raise exception 'kb_baseline_frozen_guard: %.% holds rows of a frozen snapshot; TRUNCATE is not allowed',
        tg_table_schema, tg_table_name using errcode = '55000';
    end if;
    return null;
  end if;

  if tg_table_name = 'kb_chunks' then
    if tg_op in ('UPDATE', 'DELETE') and old.layer = 'baseline' then
      v_old := old.snapshot_id;
    end if;
    if tg_op in ('INSERT', 'UPDATE') and new.layer = 'baseline' then
      v_new := new.snapshot_id;
    end if;
    if tg_op = 'UPDATE' and v_old is not null
       and (to_jsonb(new) - c_embedding) = (to_jsonb(old) - c_embedding) then
      return new;                                  -- embedding-only update
    end if;
  else
    if tg_op in ('UPDATE', 'DELETE') then
      v_old := old.snapshot_id;
    end if;
    if tg_op in ('INSERT', 'UPDATE') then
      v_new := new.snapshot_id;
    end if;
  end if;

  select string_agg(distinct s.id || ' (' || s.status || ')', ', ')
    into v_locked
  from public.kb_snapshots s
  where s.id in (v_old, v_new) and s.status <> 'building';

  if v_locked is not null then
    raise exception 'kb_baseline_frozen_guard: snapshot % is immutable (% on %.%); build a new snapshot_id instead',
      v_locked, tg_op, tg_table_schema, tg_table_name using errcode = '55000';
  end if;

  return case when tg_op = 'DELETE' then old else new end;
end
$$;
revoke all on function public.kb_baseline_frozen_guard() from public, anon, authenticated;

do $$
declare
  t text;
begin
  foreach t in array array['kb_baseline_notices', 'kb_baseline_procedures', 'kb_baseline_stats', 'kb_patterns'] loop
    execute format('drop trigger if exists kb_baseline_frozen_guard on public.%I', t);
    execute format('create trigger kb_baseline_frozen_guard before insert or update or delete on public.%I
                    for each row execute function public.kb_baseline_frozen_guard()', t);
    execute format('drop trigger if exists kb_baseline_frozen_guard_truncate on public.%I', t);
    execute format('create trigger kb_baseline_frozen_guard_truncate before truncate on public.%I
                    for each statement execute function public.kb_baseline_frozen_guard()', t);
  end loop;
end
$$;

-- A frozen snapshot keeps its manifest. Allowed later: active on/off, frozen -> retired, and setting the
-- Storage file path and hash once. Frozen or retired snapshots cannot be deleted.
create or replace function public.kb_snapshots_guard()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  c_mutable constant text[] := array['active', 'status', 'file_path', 'file_sha256'];
begin
  if tg_op = 'DELETE' then
    if old.status <> 'building' then
      raise exception 'kb_snapshots_guard: snapshot % is %; retire it instead of deleting it', old.id, old.status
        using errcode = '55000';
    end if;
    return old;
  end if;

  if old.status = 'building' then
    return new;
  end if;

  if (to_jsonb(new) - c_mutable) <> (to_jsonb(old) - c_mutable)
     or not (new.status = old.status or (old.status = 'frozen' and new.status = 'retired'))
     or (old.file_path is not null and new.file_path is distinct from old.file_path)
     or (old.file_sha256 is not null and new.file_sha256 is distinct from old.file_sha256) then
    raise exception 'kb_snapshots_guard: snapshot % is %; only active, frozen -> retired and the file path/hash (once) may change',
      old.id, old.status using errcode = '55000';
  end if;
  return new;
end
$$;
revoke all on function public.kb_snapshots_guard() from public, anon, authenticated;

drop trigger if exists kb_snapshots_guard on public.kb_snapshots;
create trigger kb_snapshots_guard before update or delete on public.kb_snapshots
  for each row execute function public.kb_snapshots_guard();

-- ---------------------------------------------------------------------------------------------------------
-- RLS: public read, no public writes (the service role bypasses RLS)
-- ---------------------------------------------------------------------------------------------------------

do $$
declare
  t text;
begin
  foreach t in array array['kb_snapshots', 'kb_baseline_notices', 'kb_baseline_procedures', 'kb_baseline_stats', 'kb_patterns'] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('drop policy if exists "public read" on public.%I', t);
    execute format('create policy "public read" on public.%I for select to anon, authenticated using (true)', t);
    execute format('revoke insert, update, delete, truncate on public.%I from anon, authenticated', t);
  end loop;
end
$$;

-- ---------------------------------------------------------------------------------------------------------
-- Harvest plumbing (Edge Function kb-baseline-harvest), same pattern as ted-sync
-- ---------------------------------------------------------------------------------------------------------

-- Used by the Edge Function (service_role only) to check the x-baseline-secret header.
create or replace function public.kb_baseline_check_secret(candidate text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from vault.decrypted_secrets
    where name = 'kb_baseline_secret' and decrypted_secret = candidate
  );
$$;
revoke all on function public.kb_baseline_check_secret(text) from public, anon, authenticated;
grant execute on function public.kb_baseline_check_secret(text) to service_role;

-- Calls the Edge Function from the database (one run = about p_budget_seconds of harvesting):
--   select public.kb_baseline_trigger();                         -- resume baseline_v1
--   select public.kb_baseline_trigger('baseline_v1', 100, true); -- restart the cursor (rows are kept)
create or replace function public.kb_baseline_trigger(p_snapshot text default 'baseline_v1',
                                                      p_budget_seconds int default 100,
                                                      p_restart boolean default false)
returns bigint
language sql
security definer
set search_path = ''
as $$
  select net.http_post(
    url := (select decrypted_secret from vault.decrypted_secrets where name = 'kb_baseline_url'),
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-baseline-secret', (select decrypted_secret from vault.decrypted_secrets where name = 'kb_baseline_secret')
    ),
    body := jsonb_build_object('snapshot', p_snapshot, 'budgetSeconds', p_budget_seconds, 'restart', p_restart),
    timeout_milliseconds := 140000
  );
$$;
revoke all on function public.kb_baseline_trigger(text, int, boolean) from public, anon, authenticated;

-- Insert normalised notices of one harvest query. A notice returned by several queries is stored once
-- and remembers every query key.
create or replace function public.kb_baseline_ingest(p_snapshot text, p_query text, p_kind text, p_rows jsonb)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_n integer;
begin
  if p_kind not in ('competition', 'result', 'modification') then
    raise exception 'kb_baseline_ingest: unknown kind %', p_kind;
  end if;

  insert into public.kb_baseline_notices as t (
    snapshot_id, publication_number, kind, queries, procedure_id, notice_identifier, notice_type,
    publication_date, legal_basis, procedure_type, contract_nature, buyer_name, buyer_identifier, buyer_country,
    buyer_legal_type, main_activity, cpv_codes, title_en, title_original, description, original_language,
    estimated_value, estimated_currency, estimated_value_source, deadline, deadline_kind, result_value,
    result_currency, bids_received, winners, decision_date, place_of_performance, direct_award_justification, raw)
  select p_snapshot, r.publication_number, p_kind, array[p_query], r.procedure_id, r.notice_identifier,
         r.notice_type, r.publication_date, r.legal_basis, r.procedure_type, coalesce(r.contract_nature, '{}'),
         r.buyer_name, r.buyer_identifier, r.buyer_country, r.buyer_legal_type, r.main_activity,
         coalesce(r.cpv_codes, '{}'), r.title_en, r.title_original, r.description, r.original_language,
         r.estimated_value, r.estimated_currency, r.estimated_value_source, r.deadline, r.deadline_kind,
         r.result_value, r.result_currency, r.bids_received, coalesce(r.winners, '[]'), r.decision_date,
         coalesce(r.place_of_performance, '{}'), r.direct_award_justification, r.raw
  from jsonb_to_recordset(p_rows) as r(
    publication_number text, procedure_id text, notice_identifier text, notice_type text, publication_date date,
    legal_basis text, procedure_type text, contract_nature text[], buyer_name text, buyer_identifier text,
    buyer_country text, buyer_legal_type text, main_activity text, cpv_codes text[], title_en text,
    title_original text, description text, original_language text, estimated_value numeric,
    estimated_currency text, estimated_value_source text, deadline timestamptz, deadline_kind text,
    result_value numeric, result_currency text, bids_received numeric, winners jsonb, decision_date date,
    place_of_performance text[], direct_award_justification text, raw jsonb)
  where r.publication_number is not null and r.raw is not null
  on conflict (snapshot_id, publication_number) do update
    set queries = (select array_agg(distinct q order by q) from unnest(t.queries || excluded.queries) q)
    where not (excluded.queries <@ t.queries);
  get diagnostics v_n = row_count;
  return v_n;
end
$$;
revoke all on function public.kb_baseline_ingest(text, text, text, jsonb) from public, anon, authenticated;
grant execute on function public.kb_baseline_ingest(text, text, text, jsonb) to service_role;

-- Second, small field pass: result status, previous notice references, lots and quantities.
create or replace function public.kb_baseline_ingest_extra(p_snapshot text, p_rows jsonb)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_n integer;
begin
  update public.kb_baseline_notices t set
    notice_version = r.notice_version,
    winner_selection_status = coalesce(r.winner_selection_status, '{}'),
    non_award_justification = coalesce(r.non_award_justification, '{}'),
    previous_notice_ids = coalesce(r.previous_notice_ids, '{}'),
    modified_notice_ids = coalesce(r.modified_notice_ids, '{}'),
    tendering_parties = coalesce(r.tendering_parties, '{}'),
    n_lots = r.n_lots,
    quantity = r.quantity,
    quantity_unit = r.quantity_unit,
    contract_conclusion_date = r.contract_conclusion_date,
    raw = t.raw || coalesce(r.raw, '{}'),
    extra_fetched = true
  from jsonb_to_recordset(p_rows) as r(
    publication_number text, notice_version int, winner_selection_status text[], non_award_justification text[],
    previous_notice_ids text[], modified_notice_ids text[], tendering_parties text[], n_lots int,
    quantity numeric, quantity_unit text, contract_conclusion_date date, raw jsonb)
  where t.snapshot_id = p_snapshot and t.publication_number = r.publication_number;
  get diagnostics v_n = row_count;
  return v_n;
end
$$;
revoke all on function public.kb_baseline_ingest_extra(text, jsonb) from public, anon, authenticated;
grant execute on function public.kb_baseline_ingest_extra(text, jsonb) to service_role;

-- Targets for the linking passes: procedure ids of results/modifications without a competition notice in
-- the snapshot ('procedure'), or tender publication numbers referenced by previous-notice-id-proc that are
-- not in the snapshot ('previous'). Ordered, after p_after, so the harvest can page through them.
create or replace function public.kb_baseline_link_targets(p_snapshot text, p_mode text, p_after text default null,
                                                           p_limit int default 40)
returns text[]
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce(array_agg(x order by x), '{}')
  from (
    select x from (
      select distinct r.procedure_id as x
      from public.kb_baseline_notices r
      where p_mode = 'procedure' and r.snapshot_id = p_snapshot and r.kind in ('result', 'modification')
        and r.procedure_id is not null and r.procedure_id > coalesce(p_after, '')
        and not exists (select 1 from public.kb_baseline_notices c
                        where c.snapshot_id = p_snapshot and c.kind = 'competition' and c.procedure_id = r.procedure_id)
      union
      select distinct p.x
      from public.kb_baseline_notices r, unnest(r.previous_notice_ids) as p(x)
      where p_mode = 'previous' and r.snapshot_id = p_snapshot and r.kind in ('result', 'modification')
        and p.x ~ '^[0-9]{1,8}-[0-9]{4}$' and p.x > coalesce(p_after, '')
        and not exists (select 1 from public.kb_baseline_notices c
                        where c.snapshot_id = p_snapshot and c.publication_number = p.x)
    ) u
    order by x
    limit greatest(p_limit, 1)
  ) s;
$$;
revoke all on function public.kb_baseline_link_targets(text, text, text, int) from public, anon, authenticated;
grant execute on function public.kb_baseline_link_targets(text, text, text, int) to service_role;

-- ---------------------------------------------------------------------------------------------------------
-- baseline_v1: scope checked in the TED API on 2026-10-30 (cutoff = harvest day)
-- ---------------------------------------------------------------------------------------------------------

insert into public.kb_snapshots (id, cutoff_date, status, queries, notes)
values (
  'baseline_v1', '2026-10-30', 'building',
  jsonb_build_array(
    jsonb_build_object('key', 'a_comp', 'kind', 'competition', 'query',
      'classification-cpv IN (35300000) AND notice-type IN (qu-sy pin-cfc-standard subco cn-desg cn-social cn-standard pin-cfc-social) AND PD<=20261030'),
    jsonb_build_object('key', 'a_res', 'kind', 'result', 'query',
      'classification-cpv IN (35300000) AND notice-type IN (can-standard can-desg can-social can-tran) AND PD<=20261030'),
    jsonb_build_object('key', 'a_mod', 'kind', 'modification', 'query',
      'classification-cpv IN (35300000) AND notice-type IN (can-modif) AND PD<=20261030'),
    jsonb_build_object('key', 'b_res', 'kind', 'result', 'query',
      'legal-basis=32009L0081 AND PD>=20231101 AND PD<=20261030 AND notice-type IN (can-standard can-desg can-social can-tran)'),
    jsonb_build_object('key', 'link_procedure', 'kind', 'competition', 'query',
      'procedure-identifier IN (<procedure ids of harvested results without a tender>) AND notice-type IN (qu-sy pin-cfc-standard subco cn-desg cn-social cn-standard pin-cfc-social) AND PD<=20261030'),
    jsonb_build_object('key', 'link_previous', 'kind', 'competition', 'query',
      'publication-number IN (<previous-notice-id-proc of harvested results>) AND notice-type IN (qu-sy pin-cfc-standard subco cn-desg cn-social cn-standard pin-cfc-social) AND PD<=20261030')
  ),
  'Core: CPV group 353 (weapons and ammunition), every contract nature, all years. Buyer behaviour: defence directive results since eForms (Nov 2023). Plus the tenders of all harvested results (by procedure id and previous-notice reference).'
)
on conflict (id) do nothing;
