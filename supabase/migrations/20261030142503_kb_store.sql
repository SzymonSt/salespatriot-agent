-- WS1 RAG store: kb_chunks, chunk builders, kb_refresh(), kb_search(), cron kb-refresh.
-- Contract: docs/agent-context-rag-implementation.md §3.2, §3.3 and §5 WS1.
--
-- Layers (decision 8): kb_refresh() writes only layer 'ted_live' (tender, award, contract) and 'platform'
-- (supplier_profile, buyer_profile, catalogue_item, conversation). It never touches layer 'baseline'
-- (WS8 writes those once) and never touches kinds it does not build (kb_doc, pattern).
-- Tender and award chunks are built only for notices published after the active snapshot's cutoff_date
-- (public.kb_snapshots, WS8); without an active snapshot every TED row is ted_live. Contract chunks are
-- lot-level wins of the profiled suppliers, which the baseline does not hold, so they ignore the cutoff.
--
-- Family and calibre come from product_items (family_id, attrs->>'calibre'). When the classifier has not
-- decided a family, class_id falls back to the item's class or the CPV class; the "What:" line says
-- "(family not decided)", so the chunk is re-embedded automatically once a family is assigned.

create extension if not exists vector with schema extensions;

-- ---------------------------------------------------------------- table

create table public.kb_chunks (
  id uuid primary key default gen_random_uuid(),
  kind text not null check (kind in ('tender','award','contract','supplier_profile','buyer_profile',
                                     'catalogue_item','conversation','kb_doc','pattern')),
  source_key text not null,
  ref text not null,
  title text not null,
  content text not null,
  facts jsonb not null default '{}',
  publication_number text,
  supplier_id text,
  buyer_id text,
  item_ids uuid[] not null default '{}',
  class_id text,
  family_id text,
  calibre text,
  cpv_codes text[] not null default '{}',
  country char(3),
  occurred_at date,
  layer text not null check (layer in ('baseline','ted_live','platform')),
  snapshot_id text,
  check ((layer = 'baseline') = (snapshot_id is not null)),
  visibility text not null default 'public' check (visibility in ('public','parties')),
  party_ids text[] not null default '{}',
  content_hash text generated always as (md5(content)) stored,
  embedded_hash text,                           -- content_hash at the time of embedding
  embedding extensions.vector(768),
  embedding_model text,
  embedded_at timestamptz,
  fts tsvector generated always as (to_tsvector('simple', coalesce(title, '') || ' ' || content)) stored,
  updated_at timestamptz not null default now(),
  constraint kb_chunks_natural_key unique nulls not distinct (kind, source_key, layer, snapshot_id)
);
comment on table public.kb_chunks is
  'RAG store (docs/agent-context-rag-implementation.md §3.2). Built by kb_refresh() (ted_live, platform), WS8 (baseline), '
  'kb_doc loader (WS5). Embeddings by Edge Function kb-embed.';

create index kb_chunks_embedding on public.kb_chunks using hnsw (embedding extensions.vector_cosine_ops);
create index kb_chunks_fts on public.kb_chunks using gin (fts);
create index kb_chunks_family on public.kb_chunks (family_id, calibre);
create index kb_chunks_cpv on public.kb_chunks using gin (cpv_codes);
create index kb_chunks_pending on public.kb_chunks (updated_at)
  where embedding is null or embedded_hash is distinct from content_hash;
create index kb_chunks_publication on public.kb_chunks (publication_number);
create index kb_chunks_supplier on public.kb_chunks (supplier_id);

alter table public.kb_chunks enable row level security;
create policy "public read" on public.kb_chunks for select to anon, authenticated using (visibility = 'public');
-- Writes only through the service role and the security-definer functions below.
revoke insert, update, delete, truncate, references, trigger on public.kb_chunks from anon, authenticated;

-- ---------------------------------------------------------------- builder row type and helpers

create type public.kb_chunk_src as (
  kind text, source_key text, ref text, title text, content text, facts jsonb,
  publication_number text, supplier_id text, buyer_id text, item_ids uuid[],
  class_id text, family_id text, calibre text, cpv_codes text[], country text, occurred_at date,
  layer text, visibility text, party_ids text[]
);

-- 1509419.1 + 'PLN' -> '1,509,419.1 PLN'. Locale independent.
create or replace function public.kb_fmt_money(p_value numeric, p_currency text)
returns text language sql immutable security definer set search_path = '' as $$
  select case when p_value is null then null
    else rtrim(to_char(p_value, 'FM999,999,999,999,990.99'), '.') || coalesce(' ' || nullif(btrim(p_currency), ''), '') end
$$;

create or replace function public.kb_fmt_ts(p_ts timestamptz)
returns text language sql immutable security definer set search_path = '' as $$
  select case when p_ts is null then null else to_char(p_ts at time zone 'UTC', 'YYYY-MM-DD HH24:MI') || ' UTC' end
$$;

-- CPV class fallback (3531 misc_weapons, 3532 firearms, 3533 ammunition, 3534 parts): first matching code wins.
create or replace function public.kb_cpv_class(p_cpv text[])
returns text language sql immutable security definer set search_path = '' as $$
  select case left(u.c, 4) when '3531' then 'misc_weapons' when '3532' then 'firearms'
                           when '3533' then 'ammunition' when '3534' then 'parts' end
  from unnest(coalesce(p_cpv, '{}'::text[])) with ordinality u(c, o)
  where left(u.c, 4) in ('3531', '3532', '3533', '3534')
  order by u.o
  limit 1
$$;

-- Deterministic "key: value; key: value" (keys in C order, arrays joined with ', ', empty values skipped).
create or replace function public.kb_jsonb_text(p_value jsonb, p_max int default 400)
returns text language sql immutable security definer set search_path = '' as $$
  select nullif(left(string_agg(e.key || ': ' || e.val, '; ' order by e.key collate "C"), p_max), '')
  from (
    select j.key,
           case jsonb_typeof(j.value)
             when 'array' then (select string_agg(case jsonb_typeof(x) when 'string' then x #>> '{}' else x::text end,
                                                  ', ' order by o)
                                from jsonb_array_elements(j.value) with ordinality a(x, o))
             when 'string' then j.value #>> '{}'
             when 'null' then null
             else j.value::text
           end as val
    from jsonb_each(case when jsonb_typeof(p_value) = 'object' then p_value else '{}'::jsonb end) j
  ) e
  where nullif(btrim(e.val), '') is not null
$$;

-- "What:" line: family name, else class name marked undecided, plus calibre.
create or replace function public.kb_what(p_family_id text, p_class_id text, p_calibre text)
returns text language sql stable security definer set search_path = '' as $$
  select concat_ws(' ',
    coalesce((select f.name from public.product_families f where f.id = p_family_id),
             (select c.name || ' (family not decided)' from public.product_families c where c.id = p_class_id),
             'unclassified'),
    nullif(btrim(p_calibre), ''))
$$;

create or replace function public.kb_country(p_value text)
returns text language sql immutable security definer set search_path = '' as $$
  select case when upper(btrim(p_value)) ~ '^[A-Z]{3}$' then upper(btrim(p_value)) end
$$;

-- ---------------------------------------------------------------- builders (one per kind)

-- tender: ted_notices kind=competition ⨝ demand item.
create or replace function public.kb_build_tender(p_cutoff date default null, p_since timestamptz default null)
returns setof public.kb_chunk_src
language sql stable security definer set search_path = '' as $$
  select
    'tender'::text,
    'ted:' || n.publication_number,
    'ted:' || n.publication_number,
    left(coalesce(nullif(btrim(n.title_en), ''), nullif(btrim(n.title_original), ''), 'Tender ' || n.publication_number), 300),
    left(concat_ws(E'\n',
      'TENDER ' || n.publication_number || ' · ' || coalesce(n.buyer_country, 'country not stated') || ' · '
        || coalesce(nullif(btrim(n.buyer_name), ''), 'buyer not stated') || ' · '
        || coalesce(n.procedure_type, 'procedure not stated') || ' · legal basis ' || coalesce(n.legal_basis, 'not stated'),
      'What: ' || public.kb_what(i.family_id, x.class_id, x.calibre),
      'Title: ' || coalesce(nullif(btrim(n.title_en), ''), 'not published'),
      case when nullif(btrim(n.title_original), '') is not null and n.title_original is distinct from n.title_en
           then 'Original: ' || btrim(n.title_original) end,
      nullif(left(btrim(n.description), 1200), ''),
      'Deadline: ' || coalesce(public.kb_fmt_ts(coalesce(n.deadline_tender, n.deadline_request))
        || case when n.deadline_tender is not null then ' (tenders)' else ' (requests to participate)' end, 'not published'),
      'Estimated value: ' || coalesce(public.kb_fmt_money(n.estimated_value, n.estimated_currency)
        || coalesce(' (' || n.estimated_value_source || ')', ''), 'not published')
    ), 2000),
    jsonb_strip_nulls(jsonb_build_object(
      'publication_number', n.publication_number, 'ted_url', n.ted_url, 'notice_type', n.notice_type,
      'published', n.publication_date, 'buyer', n.buyer_name, 'buyer_id', i.buyer_id, 'country', n.buyer_country,
      'buyer_legal_type', n.buyer_legal_type, 'procedure_type', n.procedure_type, 'legal_basis', n.legal_basis,
      'class_id', x.class_id, 'family_id', i.family_id, 'calibre', x.calibre,
      'classification_method', i.classification_method, 'confidence', round(i.confidence::numeric, 2),
      'needs_review', i.needs_review,
      'estimated_value', n.estimated_value, 'currency', n.estimated_currency, 'value_source', n.estimated_value_source,
      'deadline_tender', n.deadline_tender, 'deadline_request', n.deadline_request,
      'next_deadline', coalesce(n.deadline_tender, n.deadline_request),
      'deadline_kind', case when n.deadline_tender is not null then 'tenders'
                            when n.deadline_request is not null then 'requests_to_participate' end,
      'is_open', greatest(coalesce(n.deadline_tender, '-infinity'), coalesce(n.deadline_request, '-infinity')) >= now()
    )),
    n.publication_number, null::text, i.buyer_id,
    case when i.id is null then '{}'::uuid[] else array[i.id] end,
    x.class_id, i.family_id, x.calibre, coalesce(n.cpv_codes, '{}'::text[]), public.kb_country(n.buyer_country),
    n.publication_date, 'ted_live'::text, 'public'::text, '{}'::text[]
  from public.ted_notices n
  left join public.product_items i on i.source_kind = 'ted_notice' and i.source_ref = 'ted:' || n.publication_number
  cross join lateral (
    select coalesce(i.class_id, public.kb_cpv_class(n.cpv_codes)) as class_id,
           nullif(btrim(i.attrs ->> 'calibre'), '') as calibre
  ) x
  where n.kind = 'competition'
    and (p_cutoff is null or n.publication_date > p_cutoff)
    and (p_since is null or n.updated_at >= p_since or i.updated_at >= p_since)
$$;

-- award: one per winner, ted_awards ⨝ ted_notices ⨝ award item. notice_value is the notice total, not a lot price.
create or replace function public.kb_build_award(p_cutoff date default null, p_since timestamptz default null)
returns setof public.kb_chunk_src
language sql stable security definer set search_path = '' as $$
  select
    'award'::text,
    'award:' || a.publication_number || '/' || a.winner_key,
    'award:' || a.publication_number || '/' || a.winner_key,
    left(a.winner_name || ' won ' || a.publication_number || ' · '
         || coalesce(nullif(btrim(n.title_en), ''), nullif(btrim(n.title_original), ''), 'award'), 300),
    left(concat_ws(E'\n',
      'AWARD ' || a.publication_number || ' · ' || coalesce(n.buyer_country, 'country not stated') || ' · buyer '
        || coalesce(nullif(btrim(n.buyer_name), ''), 'not stated') || ' → winner ' || a.winner_name
        || ' (' || coalesce(a.winner_country, 'country not stated') || ', ' || coalesce(a.winner_size, 'size not stated') || ')',
      'What: ' || public.kb_what(i.family_id, x.class_id, x.calibre),
      coalesce(nullif(btrim(n.title_en), ''), nullif(btrim(n.title_original), '')),
      nullif(left(btrim(n.description), 600), ''),
      'Notice value: ' || coalesce(public.kb_fmt_money(a.notice_value, a.currency), 'not published')
        || ' (notice total, ' || coalesce(a.co_winners::text, '?') || ' winner(s))',
      'Decided: ' || coalesce(a.decision_date::text, 'not stated')
    ), 2000),
    jsonb_strip_nulls(jsonb_build_object(
      'publication_number', a.publication_number, 'ted_url', n.ted_url, 'notice_type', n.notice_type,
      'published', n.publication_date, 'buyer', n.buyer_name, 'buyer_id', x.buyer_id, 'country', n.buyer_country,
      'procedure_type', n.procedure_type, 'legal_basis', n.legal_basis,
      'winner', a.winner_name, 'winner_identifier', a.winner_identifier, 'winner_country', a.winner_country,
      'winner_size', a.winner_size, 'winner_owner_nationality', a.winner_owner_nationality, 'supplier_id', i.supplier_id,
      'class_id', x.class_id, 'family_id', i.family_id, 'calibre', x.calibre,
      'classification_method', i.classification_method, 'confidence', round(i.confidence::numeric, 2),
      'needs_review', i.needs_review,
      'notice_value', a.notice_value, 'currency', a.currency, 'value_basis', 'notice_total',
      'co_winners', a.co_winners, 'decision_date', a.decision_date, 'bids_received', n.bids_received
    )),
    a.publication_number, i.supplier_id, x.buyer_id,
    case when i.id is null then '{}'::uuid[] else array[i.id] end,
    x.class_id, i.family_id, x.calibre, coalesce(n.cpv_codes, '{}'::text[]), public.kb_country(n.buyer_country),
    coalesce(a.decision_date, n.publication_date), 'ted_live'::text, 'public'::text, '{}'::text[]
  from public.ted_awards a
  join public.ted_notices n on n.publication_number = a.publication_number
  left join public.product_items i
    on i.source_kind = 'ted_notice' and i.source_ref = 'ted:' || a.publication_number || '/' || a.winner_key
  cross join lateral (
    select coalesce(i.class_id, public.kb_cpv_class(n.cpv_codes)) as class_id,
           nullif(btrim(i.attrs ->> 'calibre'), '') as calibre,
           public.product_bridge_match_buyer(n.buyer_identifier, n.buyer_name, n.buyer_country) as buyer_id
  ) x
  where (p_cutoff is null or n.publication_date > p_cutoff)
    and (p_since is null or a.updated_at >= p_since or n.updated_at >= p_since or i.updated_at >= p_since)
$$;

-- contract: lot-level wins of the profiled suppliers (supplier_contracts ⨝ contract item).
create or replace function public.kb_build_contract(p_since timestamptz default null)
returns setof public.kb_chunk_src
language sql stable security definer set search_path = '' as $$
  select
    'contract'::text,
    x.ref, x.ref,
    left(coalesce(s.name, c.supplier_id) || ' · ' || coalesce(x.title, c.notice_number), 300),
    left(concat_ws(E'\n',
      'CONTRACT ' || coalesce(s.name, c.supplier_id) || ' won lot ' || x.lot || ' of ' || c.notice_number
        || ' · buyer ' || coalesce(nullif(btrim(c.buyer), ''), 'not stated') || ' (' || coalesce(c.buyer_country, 'country not stated') || ')',
      'What: ' || public.kb_what(i.family_id, x.class_id, x.calibre),
      x.title,
      nullif(left(btrim(c.description), 800), ''),
      'Awarded: ' || coalesce(public.kb_fmt_money(c.awarded_value, c.awarded_currency), 'value not published')
        || ' · quantity ' || coalesce(nullif(btrim(c.quantity), ''), 'not stated')
        || ' · tenders received ' || coalesce(c.tenders_received::text, 'not stated')
        || ' · ' || coalesce(c.award_date::text, 'award date not stated')
        || case when c.framework_agreement then ' · framework agreement' else '' end
    ), 2000),
    jsonb_strip_nulls(jsonb_build_object(
      'supplier_id', c.supplier_id, 'supplier', s.name, 'notice_number', c.notice_number, 'lot_id', c.lot_id,
      'notice_type', c.notice_type, 'ted_url', c.ted_url, 'buyer', c.buyer, 'country', c.buyer_country,
      'class_id', x.class_id, 'family_id', i.family_id, 'calibre', x.calibre,
      'classification_method', i.classification_method, 'confidence', round(i.confidence::numeric, 2),
      'needs_review', i.needs_review,
      'awarded_value', c.awarded_value, 'currency', c.awarded_currency, 'value_basis', 'awarded_lot_value',
      'quantity', nullif(btrim(c.quantity), ''),
      'tenders_received', c.tenders_received, 'framework_agreement', c.framework_agreement,
      'co_winners', c.co_winners, 'award_date', c.award_date, 'published', c.published,
      'contract_signed', c.contract_signed, 'winner_confirmed', c.winner_confirmed
    )),
    c.notice_number, c.supplier_id, null::text,
    case when i.id is null then '{}'::uuid[] else array[i.id] end,
    x.class_id, i.family_id, x.calibre, coalesce(c.cpv_codes, '{}'::text[]), public.kb_country(c.buyer_country),
    coalesce(c.award_date, c.published), 'ted_live'::text, 'public'::text, '{}'::text[]
  from public.supplier_contracts c
  left join public.suppliers s on s.id = c.supplier_id
  cross join lateral (
    select coalesce(nullif(btrim(c.lot_id), ''), 'notice') as lot,
           'contract:' || c.supplier_id || '/' || c.notice_number || '/' || coalesce(nullif(btrim(c.lot_id), ''), 'notice') as ref,
           coalesce(nullif(btrim(c.title), ''), nullif(btrim(c.notice_title), ''), nullif(btrim(c.contract_title), '')) as title
  ) x0
  left join public.product_items i on i.source_kind = 'ted_lot' and i.source_ref = x0.ref
  cross join lateral (
    select x0.lot, x0.ref, x0.title,
           coalesce(i.class_id, public.kb_cpv_class(c.cpv_codes)) as class_id,
           nullif(btrim(i.attrs ->> 'calibre'), '') as calibre
  ) x
  where p_since is null or i.updated_at >= p_since or s.updated_at >= p_since or i.id is null
$$;

-- supplier_profile: suppliers ⨝ supplier_overview ⨝ ui_supplier_families.
create or replace function public.kb_build_supplier_profile()
returns setof public.kb_chunk_src
language sql stable security definer set search_path = '' as $$
  with fam as (
    select f.supplier_id, f.family_id, f.family_name, f.class_id, f.proven_lots, f.declared_items, f.calibres,
           f.last_award, f.best_evidence
    from public.ui_supplier_families f
  ),
  agg as (
    select f.supplier_id,
           string_agg(f.family_name || ': ' || f.proven_lots || ' lot(s)'
                      || case when cardinality(f.calibres) > 0 then ', ' || array_to_string(f.calibres, '/') else '' end,
                      '; ' order by f.proven_lots desc, f.family_id) filter (where f.proven_lots > 0) as proven,
           string_agg(f.family_name || ': ' || f.declared_items || ' item(s)',
                      '; ' order by f.declared_items desc, f.family_id) filter (where f.declared_items > 0) as declared,
           jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
                      'family_id', f.family_id, 'family_name', f.family_name, 'proven_lots', f.proven_lots,
                      'declared_items', f.declared_items, 'calibres', f.calibres, 'last_award', f.last_award,
                      'best_evidence', f.best_evidence))
                     order by f.proven_lots desc, f.declared_items desc, f.family_id) as families,
           (array_agg(f.family_id order by f.proven_lots desc, f.declared_items desc, f.family_id))[1] as top_family,
           (array_agg(f.class_id order by f.proven_lots desc, f.declared_items desc, f.family_id))[1] as top_class
    from fam f
    group by f.supplier_id
  )
  select
    'supplier_profile'::text,
    'supplier:' || s.id, 'supplier:' || s.id,
    left(s.name || ' (' || coalesce(s.country, 'country not stated') || ')', 300),
    left(concat_ws(E'\n',
      'SUPPLIER ' || s.name || ' (' || coalesce(s.country, 'country not stated') || ', '
        || coalesce(s.company_size, 'size not stated') || ') id ' || s.id,
      nullif(left(btrim(s.summary), 600), ''),
      'Proven families: ' || coalesce(g.proven, 'none on record'),
      'Declared families: ' || coalesce(g.declared, 'none declared'),
      'Due diligence: ' || coalesce(to_char(o.total_score, 'FM990.0') || '/10 (' || coalesce(o.verdict, 'no verdict')
        || ', confidence ' || coalesce(o.confidence, 'not stated') || ')', 'not assessed'),
      'KYC: ' || coalesce(o.kyc_status, 'not checked')
    ), 2000),
    jsonb_strip_nulls(jsonb_build_object(
      'supplier_id', s.id, 'name', s.name, 'legal_name', s.legal_name, 'country', s.country, 'city', s.city,
      'size', s.company_size, 'website', s.website, 'families', coalesce(g.families, '[]'::jsonb),
      'dd_total', o.total_score, 'dd_verdict', o.verdict, 'dd_confidence', o.confidence,
      'red_flag_count', o.red_flag_count, 'kyc_status', o.kyc_status, 'kyc_refresh_due', o.refresh_due,
      'open_kyc_items', o.open_kyc_items, 'assessed_at', o.assessed_at
    )),
    null::text, s.id, null::text, '{}'::uuid[],
    g.top_class, g.top_family, null::text,
    coalesce((select array_agg(e ->> 'code' order by u.o)
              from jsonb_array_elements(case when jsonb_typeof(s.top_cpv) = 'array' then s.top_cpv else '[]'::jsonb end)
                   with ordinality u(e, o)
              where e ->> 'code' is not null), '{}'::text[]),
    public.kb_country(s.country), null::date, 'platform'::text, 'public'::text, '{}'::text[]
  from public.suppliers s
  left join public.supplier_overview o on o.id = s.id
  left join agg g on g.supplier_id = s.id
$$;

-- buyer_profile: buyers + their competition notices (demand items linked by the bridge).
create or replace function public.kb_build_buyer_profile()
returns setof public.kb_chunk_src
language sql stable security definer set search_path = '' as $$
  with t as (
    select i.buyer_id, n.publication_number, n.publication_date, n.procedure_type,
           coalesce(i.family_id, i.class_id, public.kb_cpv_class(n.cpv_codes)) as fam_key
    from public.product_items i
    join public.ted_notices n on n.publication_number = i.publication_number and n.kind = 'competition'
    where i.source_kind = 'ted_notice' and i.side = 'demand' and i.buyer_id is not null
  ),
  fam as (
    select t.buyer_id, t.fam_key, count(*) as n from t where t.fam_key is not null group by 1, 2
  ),
  proc as (
    select t.buyer_id, t.procedure_type, count(*) as n from t where t.procedure_type is not null group by 1, 2
  ),
  agg as (
    select b.id as buyer_id,
           (select count(*) from t where t.buyer_id = b.id) as n,
           (select min(t.publication_date) from t where t.buyer_id = b.id) as first_date,
           (select max(t.publication_date) from t where t.buyer_id = b.id) as last_date,
           (select string_agg(coalesce(pf.name, f.fam_key) || ' ' || f.n, ', ' order by f.n desc, f.fam_key)
              from (select * from fam where fam.buyer_id = b.id order by fam.n desc, fam.fam_key limit 6) f
              left join public.product_families pf on pf.id = f.fam_key) as fam_txt,
           (select jsonb_agg(jsonb_build_object('family_id', f.fam_key, 'tenders', f.n) order by f.n desc, f.fam_key)
              from fam f where f.buyer_id = b.id) as fam_json,
           (select string_agg(p.procedure_type || ' ' || p.n, ', ' order by p.n desc, p.procedure_type)
              from (select * from proc where proc.buyer_id = b.id order by proc.n desc, proc.procedure_type limit 3) p) as proc_txt,
           (select jsonb_agg(jsonb_build_object('procedure_type', p.procedure_type, 'tenders', p.n) order by p.n desc, p.procedure_type)
              from proc p where p.buyer_id = b.id) as proc_json,
           (select f.fam_key from fam f where f.buyer_id = b.id order by f.n desc, f.fam_key limit 1) as top_fam
    from public.buyers b
  )
  select
    'buyer_profile'::text,
    'buyer:' || b.id, 'buyer:' || b.id,
    left(b.name || ' (' || coalesce(b.country, 'country not stated') || ')', 300),
    left(concat_ws(E'\n',
      'BUYER ' || b.name || ' (' || coalesce(b.country, 'country not stated') || ', ' || coalesce(b.legal_type, 'type not stated')
        || ') id ' || b.id || ' · '
        || case when g.n > 0 then g.n || ' weapons/ammunition tenders on TED since ' || extract(year from g.first_date)::int
                else 'no weapons/ammunition tenders on record' end
        || ' · families ' || coalesce(g.fam_txt, 'none on record')
        || ' · typical procedures ' || coalesce(g.proc_txt, 'not known'),
      nullif(left(btrim(b.summary), 800), '')
    ), 2000),
    jsonb_strip_nulls(jsonb_build_object(
      'buyer_id', b.id, 'name', b.name, 'legal_name', b.legal_name, 'country', b.country, 'city', b.city,
      'legal_type', b.legal_type, 'main_activity', b.main_activity, 'tenders', g.n,
      'first_tender', g.first_date, 'last_tender', g.last_date, 'families', coalesce(g.fam_json, '[]'::jsonb),
      'procedures', coalesce(g.proc_json, '[]'::jsonb), 'ted_notices_total', b.ted_activity -> 'notices_total'
    )),
    null::text, null::text, b.id, '{}'::uuid[],
    coalesce((select pf.parent_id from public.product_families pf where pf.id = g.top_fam and pf.parent_id is not null),
             (select pf.id from public.product_families pf where pf.id = g.top_fam and pf.parent_id is null)),
    (select pf.id from public.product_families pf where pf.id = g.top_fam and pf.parent_id is not null),
    null::text,
    coalesce((select array_agg(e ->> 'code' order by u.o)
              from jsonb_array_elements(case when jsonb_typeof(b.top_cpv) = 'array' then b.top_cpv else '[]'::jsonb end)
                   with ordinality u(e, o)
              where e ->> 'code' is not null), '{}'::text[]),
    public.kb_country(b.country), g.last_date, 'platform'::text, 'public'::text, '{}'::text[]
  from public.buyers b
  join agg g on g.buyer_id = b.id
$$;

-- catalogue_item: product_items source_kind=catalogue ⨝ suppliers.
create or replace function public.kb_build_catalogue_item(p_since timestamptz default null)
returns setof public.kb_chunk_src
language sql stable security definer set search_path = '' as $$
  select
    'catalogue_item'::text,
    i.source_ref, i.source_ref,
    left(coalesce(s.name, i.org_ref, i.supplier_id, 'supplier') || ' · ' || coalesce(nullif(btrim(i.title), ''), 'catalogue item'), 300),
    left(concat_ws(E'\n',
      'CATALOGUE ' || coalesce(s.name, i.org_ref, i.supplier_id, 'supplier not stated') || ' · '
        || public.kb_what(i.family_id, x.class_id, x.calibre),
      coalesce(nullif(btrim(i.title), ''), 'untitled item'),
      nullif(left(btrim(i.source_text), 800), ''),
      'Commercial: ' || coalesce(public.kb_jsonb_text(i.extra -> 'catalogue' -> 'commercial', 400), 'not stated'),
      'Evidence: ' || coalesce(i.verification_status, 'not stated')
    ), 2000),
    jsonb_strip_nulls(jsonb_build_object(
      'supplier_id', i.supplier_id, 'supplier', coalesce(s.name, i.org_ref), 'item_id', i.id,
      'class_id', x.class_id, 'family_id', i.family_id, 'calibre', x.calibre, 'attrs', i.attrs,
      'classification_method', i.classification_method, 'confidence', round(i.confidence::numeric, 2),
      'needs_review', i.needs_review,
      'category', i.extra -> 'catalogue' ->> 'category', 'unit', i.extra -> 'catalogue' ->> 'unit',
      'verification_status', i.verification_status, 'commercial', i.extra -> 'catalogue' -> 'commercial',
      'track_record', i.extra -> 'catalogue' -> 'track_record'
    )),
    null::text, i.supplier_id, null::text, array[i.id],
    x.class_id, i.family_id, x.calibre, coalesce(i.cpv_codes, '{}'::text[]), public.kb_country(s.country),
    i.observed_at::date, 'platform'::text,
    case when i.visibility = 'public' then 'public' else 'parties' end,
    case when i.visibility = 'public' or i.supplier_id is null then '{}'::text[] else array[i.supplier_id] end
  from public.product_items i
  left join public.suppliers s on s.id = i.supplier_id
  cross join lateral (
    select coalesce(i.class_id, public.kb_cpv_class(i.cpv_codes)) as class_id,
           nullif(btrim(i.attrs ->> 'calibre'), '') as calibre
  ) x
  where i.source_kind = 'catalogue'
    and (p_since is null or i.updated_at >= p_since or s.updated_at >= p_since)
$$;

-- conversation: agent_conversations + agent_messages (+ deal_outcomes) from WS4. Deal cards of finished
-- conversations only; visible to the two parties. Dynamic SQL so this works before WS4's tables exist.
create or replace function public.kb_build_conversation(p_since timestamptz default null)
returns setof public.kb_chunk_src
language plpgsql stable security definer set search_path = '' as $fn$
declare
  v_outcomes text := case when to_regclass('public.deal_outcomes') is not null
    then 'left join public.deal_outcomes o on o.conversation_id = c.id'
    else 'left join lateral (select null::numeric as final_price, null::text as currency, null::boolean as delivered_on_time,
                                    null::text as quality, null::text as notes, null::timestamptz as created_at) o on true' end;
begin
  if to_regclass('public.agent_conversations') is null or to_regclass('public.agent_messages') is null then
    return;
  end if;
  return query execute format($q$
    select
      'conversation'::text,
      'conv:' || c.id::text, 'conv:' || c.id::text,
      left('Deal ' || c.status || ' · ' || coalesce(b.name, c.buyer_id, 'buyer') || ' ↔ '
           || coalesce(s.name, c.supplier_id, 'supplier') || coalesce(' · ' || c.publication_number, ''), 300),
      left(concat_ws(E'\n',
        'DEAL ' || c.status || ' · buyer ' || coalesce(b.name, c.buyer_id, 'not stated') || ' ↔ supplier '
          || coalesce(s.name, c.supplier_id, 'not stated') || ' · ' || public.kb_what(x.family_id, x.class_id, x.calibre),
        case when c.publication_number is not null
             then 'Tender: ' || c.publication_number || coalesce(' · ' || nullif(btrim(n.title_en), ''), '') end,
        'Asked: ' || coalesce(m.asked, 'no request recorded'),
        'Quoted: ' || coalesce(m.quoted, 'no quote recorded'),
        'Outcome: ' || concat_ws('; ', c.status, public.kb_jsonb_text(c.outcome, 400),
          'final price ' || public.kb_fmt_money(o.final_price, o.currency),
          'delivered on time ' || case o.delivered_on_time when true then 'yes' when false then 'no' end,
          'quality ' || nullif(btrim(o.quality), ''), nullif(left(btrim(o.notes), 300), ''))
      ), 2000),
      jsonb_strip_nulls(jsonb_build_object(
        'conversation_id', c.id, 'publication_number', c.publication_number, 'buyer_id', c.buyer_id,
        'supplier_id', c.supplier_id, 'family_id', x.family_id, 'calibre', x.calibre, 'status', c.status,
        'demo', c.demo, 'messages', m.n_messages, 'created_at', c.created_at, 'closed_at', c.closed_at,
        'outcome', c.outcome, 'final_price', o.final_price, 'currency', o.currency,
        'delivered_on_time', o.delivered_on_time, 'quality', o.quality
      )),
      c.publication_number, c.supplier_id, c.buyer_id, '{}'::uuid[],
      x.class_id, x.family_id, x.calibre, coalesce(n.cpv_codes, '{}'::text[]),
      public.kb_country(coalesce(n.buyer_country, b.country)),
      coalesce(c.closed_at, c.created_at)::date, 'platform'::text, 'parties'::text,
      array_remove(array[c.buyer_id, c.supplier_id], null)
    from public.agent_conversations c
    left join public.buyers b on b.id = c.buyer_id
    left join public.suppliers s on s.id = c.supplier_id
    left join public.ted_notices n on n.publication_number = c.publication_number
    left join public.product_items i on i.source_kind = 'ted_notice' and i.source_ref = 'ted:' || c.publication_number
    %s
    cross join lateral (
      select coalesce(c.family_id, i.family_id) as family_id,
             coalesce((select f.parent_id from public.product_families f where f.id = c.family_id),
                      i.class_id, public.kb_cpv_class(n.cpv_codes)) as class_id,
             nullif(btrim(i.attrs ->> 'calibre'), '') as calibre
    ) x
    cross join lateral (
      select
        (select left(coalesce(nullif(btrim(mm.text), ''), public.kb_jsonb_text(mm.payload, 400)), 400)
           from public.agent_messages mm where mm.conversation_id = c.id and mm.type = 'rfq'
           order by mm.seq limit 1) as asked,
        (select left(concat_ws(' · ', nullif(btrim(mm.text), ''), public.kb_jsonb_text(mm.payload, 300)), 600)
           from public.agent_messages mm where mm.conversation_id = c.id and mm.type in ('quote', 'counter')
           order by mm.seq desc limit 1) as quoted,
        (select count(*) from public.agent_messages mm where mm.conversation_id = c.id) as n_messages,
        (select max(mm.created_at) from public.agent_messages mm where mm.conversation_id = c.id) as last_at
    ) m
    where (c.closed_at is not null or c.status in ('accepted', 'rejected', 'escalated', 'closed'))
      and ($1 is null or c.created_at >= $1 or c.closed_at >= $1 or m.last_at >= $1 or o.created_at >= $1)
  $q$, v_outcomes) using p_since;
end
$fn$;

-- Dispatcher used by kb_refresh().
create or replace function public.kb_build(p_kind text, p_cutoff date default null, p_since timestamptz default null)
returns setof public.kb_chunk_src
language plpgsql stable security definer set search_path = '' as $$
begin
  case p_kind
    when 'tender' then return query select * from public.kb_build_tender(p_cutoff, p_since);
    when 'award' then return query select * from public.kb_build_award(p_cutoff, p_since);
    when 'contract' then return query select * from public.kb_build_contract(p_since);
    when 'supplier_profile' then return query select * from public.kb_build_supplier_profile();
    when 'buyer_profile' then return query select * from public.kb_build_buyer_profile();
    when 'catalogue_item' then return query select * from public.kb_build_catalogue_item(p_since);
    when 'conversation' then return query select * from public.kb_build_conversation(p_since);
    else raise exception 'kb_build: unknown kind %', p_kind;
  end case;
end
$$;

-- ---------------------------------------------------------------- kb_refresh

-- Upserts chunks on (kind, source_key, layer, snapshot_id). Unchanged rows are not written; updated_at moves only
-- when content or facts change, and a content change clears the embedding (kb-embed re-embeds it).
-- Full run (p_since null): chunks of a rebuilt kind whose source is gone are removed (ted_live/platform only).
-- Returns {"<kind>": rows, ..., "changes": {"<kind>": {inserted, updated, deleted}}, "cutoff", "errors", "ms"}.
create or replace function public.kb_refresh(p_kinds text[] default null, p_since timestamptz default null)
returns jsonb
language plpgsql volatile security definer set search_path = '' as $$
declare
  v_all constant text[] := array['tender', 'award', 'contract', 'supplier_profile', 'buyer_profile',
                                 'catalogue_item', 'conversation'];
  v_kinds text[] := coalesce(p_kinds, v_all);
  v_cutoff date;
  v_kind text;
  v_rows bigint; v_ins bigint; v_upd bigint; v_del bigint;
  v_counts jsonb := '{}';
  v_changes jsonb := '{}';
  v_errors jsonb := '{}';
  v_t0 timestamptz := clock_timestamp();
begin
  if exists (select 1 from unnest(v_kinds) k where k is null or k <> all (v_all)) then
    raise exception 'kb_refresh: unknown kind in % (allowed: %)', v_kinds, v_all;
  end if;

  -- Active baseline snapshot (WS8). Without it every TED row is ted_live.
  if to_regclass('public.kb_snapshots') is not null then
    begin
      execute 'select max(cutoff_date) from public.kb_snapshots where active' into v_cutoff;
    exception when others then
      v_errors := v_errors || jsonb_build_object('kb_snapshots', sqlerrm);
      v_cutoff := null;
    end;
  end if;

  foreach v_kind in array v_kinds loop
    begin
      perform pg_advisory_xact_lock(hashtext('kb_refresh:' || v_kind));

      with src as materialized (
        select * from public.kb_build(v_kind, v_cutoff, p_since) b where b.layer in ('ted_live', 'platform')
      ),
      up as (
        insert into public.kb_chunks as k (
          kind, source_key, ref, title, content, facts, publication_number, supplier_id, buyer_id, item_ids,
          class_id, family_id, calibre, cpv_codes, country, occurred_at, layer, snapshot_id, visibility, party_ids)
        select s.kind, s.source_key, s.ref, s.title, s.content, coalesce(s.facts, '{}'::jsonb), s.publication_number,
               s.supplier_id, s.buyer_id, coalesce(s.item_ids, '{}'::uuid[]), s.class_id, s.family_id, s.calibre,
               coalesce(s.cpv_codes, '{}'::text[]), s.country, s.occurred_at, s.layer, null,
               coalesce(s.visibility, 'public'), coalesce(s.party_ids, '{}'::text[])
        from src s
        on conflict (kind, source_key, layer, snapshot_id) do update set
          ref = excluded.ref, title = excluded.title, content = excluded.content, facts = excluded.facts,
          publication_number = excluded.publication_number, supplier_id = excluded.supplier_id,
          buyer_id = excluded.buyer_id, item_ids = excluded.item_ids, class_id = excluded.class_id,
          family_id = excluded.family_id, calibre = excluded.calibre, cpv_codes = excluded.cpv_codes,
          country = excluded.country, occurred_at = excluded.occurred_at, visibility = excluded.visibility,
          party_ids = excluded.party_ids,
          embedding = case when k.content is distinct from excluded.content then null else k.embedding end,
          embedding_model = case when k.content is distinct from excluded.content then null else k.embedding_model end,
          embedded_hash = case when k.content is distinct from excluded.content then null else k.embedded_hash end,
          embedded_at = case when k.content is distinct from excluded.content then null else k.embedded_at end,
          updated_at = case when k.content is distinct from excluded.content or k.facts is distinct from excluded.facts
                            then now() else k.updated_at end
        where (k.ref, k.title, k.content, k.facts, k.publication_number, k.supplier_id, k.buyer_id, k.item_ids,
               k.class_id, k.family_id, k.calibre, k.cpv_codes, k.country, k.occurred_at, k.visibility, k.party_ids)
              is distinct from
              (excluded.ref, excluded.title, excluded.content, excluded.facts, excluded.publication_number,
               excluded.supplier_id, excluded.buyer_id, excluded.item_ids, excluded.class_id, excluded.family_id,
               excluded.calibre, excluded.cpv_codes, excluded.country, excluded.occurred_at, excluded.visibility,
               excluded.party_ids)
        returning (k.xmax = 0) as inserted
      ),
      del as (
        delete from public.kb_chunks k
        where p_since is null
          and k.kind = v_kind
          and k.layer in ('ted_live', 'platform')
          and not exists (select 1 from src s where s.source_key = k.source_key and s.layer = k.layer)
        returning 1
      )
      select (select count(*) from src),
             (select count(*) from up where inserted),
             (select count(*) from up where not inserted),
             (select count(*) from del)
        into v_rows, v_ins, v_upd, v_del;

      v_counts := v_counts || jsonb_build_object(v_kind, v_rows);
      v_changes := v_changes || jsonb_build_object(v_kind,
        jsonb_build_object('inserted', v_ins, 'updated', v_upd, 'deleted', v_del));
    exception when others then
      v_errors := v_errors || jsonb_build_object(v_kind, sqlerrm);
    end;
  end loop;

  return v_counts || jsonb_build_object(
    'changes', v_changes,
    'cutoff', v_cutoff,
    'errors', v_errors,
    'ms', round(extract(epoch from clock_timestamp() - v_t0) * 1000));
end
$$;

-- ---------------------------------------------------------------- kb_search

-- Hybrid search: vector (cosine, HNSW) + full text (simple config), fused by reciprocal rank (k = 60).
-- Full text ranks rows that contain every query word first, then rows that contain some of them.
-- Without query text and embedding: filters only, newest first.
-- p_calibre is normalised with product_normalise_calibre ('9mm Para' -> '9x19'); rows without a calibre pass
-- the filter, rows with the same calibre get +0.01. p_viewer sees 'parties' rows where it is a party.
-- Returns [{ref, kind, layer, title, snippet, facts, score}] (snippet <= 300 chars).
create or replace function public.kb_search(
  p_query_embedding extensions.vector(768) default null,
  p_query_text text default null,
  p_kinds text[] default null,
  p_family_id text default null,
  p_calibre text default null,
  p_cpv_prefix text default null,
  p_country text default null,
  p_viewer text default null,
  p_limit int default 8)
returns jsonb
language plpgsql stable security definer
set search_path = ''
set hnsw.iterative_scan = 'relaxed_order'
as $$
declare
  v_text text := nullif(btrim(p_query_text), '');
  v_and tsquery;
  v_or tsquery;
  v_lex text;
  v_limit int := least(greatest(coalesce(p_limit, 8), 1), 50);
  v_kinds text[] := case when cardinality(p_kinds) > 0 then p_kinds end;
  v_family text := nullif(btrim(p_family_id), '');
  v_calibre text := nullif(btrim(p_calibre), '');
  v_cpv text := nullif(regexp_replace(coalesce(p_cpv_prefix, ''), '[^0-9]', '', 'g'), '');
  v_country text := upper(nullif(btrim(p_country), ''));
  v_viewer text := nullif(btrim(p_viewer), '');
  v_result jsonb;
begin
  if v_calibre is not null then
    v_calibre := coalesce(public.product_normalise_calibre(v_calibre), v_calibre);
  end if;

  if v_text is not null then
    v_and := websearch_to_tsquery('simple', v_text);
    if numnode(v_and) = 0 then v_and := null; end if;
    for v_lex in
      select l from unnest(tsvector_to_array(to_tsvector('simple', v_text))) l where length(l) > 1 limit 24
    loop
      v_or := case when v_or is null then plainto_tsquery('simple', v_lex) else v_or || plainto_tsquery('simple', v_lex) end;
    end loop;
    v_or := coalesce(v_or, v_and);
  end if;

  with vec as (
    select c.id, row_number() over (order by c.dist, c.id) as r
    from (
      select k.id, k.embedding operator(extensions.<=>) p_query_embedding as dist
      from public.kb_chunks k
      where p_query_embedding is not null and k.embedding is not null
        and (v_kinds is null or k.kind = any (v_kinds))
        and (v_family is null or k.family_id = v_family or k.class_id = v_family)
        and (v_calibre is null or k.calibre is null or k.calibre = v_calibre)
        and (v_cpv is null or exists (select 1 from unnest(k.cpv_codes) cc where cc like v_cpv || '%'))
        and (v_country is null or k.country = v_country)
        and (k.visibility = 'public' or (v_viewer is not null and v_viewer = any (k.party_ids)))
      order by k.embedding operator(extensions.<=>) p_query_embedding
      limit 40
    ) c
  ),
  txt as (
    select c.id, row_number() over (order by c.and_hit desc, c.rank desc, c.id) as r
    from (
      select k.id, coalesce(k.fts @@ v_and, false) as and_hit, ts_rank_cd(k.fts, v_or) as rank
      from public.kb_chunks k
      where v_or is not null and k.fts @@ v_or
        and (v_kinds is null or k.kind = any (v_kinds))
        and (v_family is null or k.family_id = v_family or k.class_id = v_family)
        and (v_calibre is null or k.calibre is null or k.calibre = v_calibre)
        and (v_cpv is null or exists (select 1 from unnest(k.cpv_codes) cc where cc like v_cpv || '%'))
        and (v_country is null or k.country = v_country)
        and (k.visibility = 'public' or (v_viewer is not null and v_viewer = any (k.party_ids)))
      order by 2 desc, 3 desc, k.id
      limit 40
    ) c
  ),
  hits as (
    select coalesce(v.id, t.id) as id, v.r as vr, t.r as tr
    from vec v full join txt t on t.id = v.id
  ),
  ranked as (
    select k.ref, k.kind, k.layer, k.title, k.content, k.facts, k.occurred_at,
           coalesce(1.0 / (60 + h.vr), 0) + coalesce(1.0 / (60 + h.tr), 0)
             + case when v_calibre is not null and k.calibre = v_calibre then 0.01 else 0 end as score
    from hits h
    join public.kb_chunks k on k.id = h.id
    union all
    select f.ref, f.kind, f.layer, f.title, f.content, f.facts, f.occurred_at,
           case when v_calibre is not null and f.calibre = v_calibre then 0.01 else 0 end
    from (
      select k.*
      from public.kb_chunks k
      where p_query_embedding is null and v_or is null
        and (v_kinds is null or k.kind = any (v_kinds))
        and (v_family is null or k.family_id = v_family or k.class_id = v_family)
        and (v_calibre is null or k.calibre is null or k.calibre = v_calibre)
        and (v_cpv is null or exists (select 1 from unnest(k.cpv_codes) cc where cc like v_cpv || '%'))
        and (v_country is null or k.country = v_country)
        and (k.visibility = 'public' or (v_viewer is not null and v_viewer = any (k.party_ids)))
      order by coalesce(k.calibre = v_calibre, false) desc, k.occurred_at desc nulls last, k.ref
      limit v_limit
    ) f
  )
  select coalesce(jsonb_agg(jsonb_build_object(
           'ref', r.ref, 'kind', r.kind, 'layer', r.layer, 'title', r.title,
           'snippet', left(r.content, 300), 'facts', r.facts, 'score', round(r.score, 5))
         order by r.score desc, r.occurred_at desc nulls last, r.ref), '[]'::jsonb)
    into v_result
  from (
    select * from ranked order by score desc, occurred_at desc nulls last, ref limit v_limit
  ) r;

  return v_result;
end
$$;

-- ---------------------------------------------------------------- grants

do $$
declare f regprocedure;
begin
  for f in
    select p.oid::regprocedure
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.proname in ('kb_fmt_money', 'kb_fmt_ts', 'kb_cpv_class', 'kb_jsonb_text', 'kb_what', 'kb_country',
                        'kb_build_tender', 'kb_build_award', 'kb_build_contract', 'kb_build_supplier_profile',
                        'kb_build_buyer_profile', 'kb_build_catalogue_item', 'kb_build_conversation', 'kb_build',
                        'kb_refresh', 'kb_search')
  loop
    execute format('revoke all on function %s from public, anon, authenticated', f);
    execute format('grant execute on function %s to service_role', f);
  end loop;
end
$$;

-- ---------------------------------------------------------------- schedule

-- :52, after ted-sync (:17), the classifier (every 10 min) and the bridge cleanup (:47).
select cron.schedule('kb-refresh', '52 * * * *', $job$ select public.kb_refresh(); $job$);
