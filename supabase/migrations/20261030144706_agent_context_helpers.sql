-- WS3: agent_context(), the one call agents use to read TED, the product ontology, company/KYC facts, RAG hits,
-- price anchors, priors and memory. Contract: docs/agent-context-rag-implementation.md §3.3, §3.4 and §5 WS3.
--
-- The pack has the same shape for every purpose; sections that do not apply are empty ([] / {} / null), never
-- missing. Lists hold ≤ p_limit items, snippets ≤ 300 chars, every list item carries a ref, evidence and price
-- anchors carry their layer. Unknown values stay null and get a warning.
--
-- Objects owned by other workstreams are optional at run time (plpgsql resolves them per call):
--   kb_search / kb_chunks (WS1), kb_snapshots / kb_baseline_stats (WS8), deal_checks / deal_check_items /
--   agent_mandates (deal_due_diligence.py --push). When missing, the section is empty and a warning says so.
--
-- Helpers are prefixed agent_ctx_ and, like agent_context, executable by service_role only.

-- ------------------------------------------------------------------------------------------ small helpers

-- jsonb number or numeric string → numeric; anything else → null.
create or replace function public.agent_ctx_num(p_value jsonb)
returns numeric
language sql
immutable
security definer
set search_path = ''
as $$
  select case jsonb_typeof(p_value)
           when 'number' then (p_value #>> '{}')::numeric
           when 'string' then case when btrim(p_value #>> '{}') ~ '^-?[0-9]+(\.[0-9]+)?$'
                                   then btrim(p_value #>> '{}')::numeric end
         end;
$$;

-- First p_limit elements of a jsonb array (other values unchanged).
create or replace function public.agent_ctx_slice(p_value jsonb, p_limit int)
returns jsonb
language sql
immutable
security definer
set search_path = ''
as $$
  select case when jsonb_typeof(p_value) = 'array'
              then coalesce((select jsonb_agg(t.x order by t.o)
                             from jsonb_array_elements(p_value) with ordinality as t(x, o)
                             where t.o <= greatest(coalesce(p_limit, 8), 0)), '[]'::jsonb)
              else p_value end;
$$;

-- ------------------------------------------------------------------------------------------ tender

-- Tender facts + scope (family, class, calibre, country, buyer) + warnings. Family from ui_tender_families;
-- fallback class from the demand item, then from the notice CPV prefix (same rule as ui_match_tender).
create or replace function public.agent_ctx_tender(p_publication_number text, p_limit int default 8)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  n          public.ted_notices%rowtype;
  v_lim      int := greatest(1, least(coalesce(p_limit, 8), 25));
  v_fams     jsonb;
  v_family   text;
  v_calibres text[] := '{}';
  v_class    text;
  v_method   text;
  v_conf     real;
  v_review   boolean;
  v_buyer_id text;
  i_class    text;
  i_method   text;
  i_conf     real;
  v_latest   timestamptz;
  w          text[] := '{}';
begin
  select * into n from public.ted_notices t where t.publication_number = p_publication_number;
  if not found then
    return jsonb_build_object('tender', null, 'scope', '{}'::jsonb,
      'warnings', jsonb_build_array(format('tender %s not found in ted_notices: tender facts unknown', p_publication_number)));
  end if;

  select coalesce(jsonb_agg(jsonb_build_object(
           'family_id', t.family_id, 'family_name', t.family_name, 'class_id', t.class_id,
           'calibre', to_jsonb(t.calibres), 'method', t.classification_method,
           'confidence', round(t.confidence::numeric, 2), 'in_focus', t.in_focus)
         order by t.in_focus desc nulls last, t.confidence desc nulls last, cardinality(t.calibres) desc, t.family_id),
         '[]'::jsonb)
    into v_fams
  from (select * from public.ui_tender_families u
        where u.publication_number = n.publication_number
        order by u.in_focus desc nulls last, u.confidence desc nulls last, cardinality(u.calibres) desc, u.family_id
        limit v_lim) t;

  if jsonb_array_length(v_fams) > 0 then
    v_family := v_fams -> 0 ->> 'family_id';
    v_class := v_fams -> 0 ->> 'class_id';
    v_method := v_fams -> 0 ->> 'method';
    v_conf := (v_fams -> 0 ->> 'confidence')::real;
    v_calibres := array(select jsonb_array_elements_text(v_fams -> 0 -> 'calibre'));
  end if;

  select bool_or(i.needs_review), max(i.buyer_id),
         (array_agg(i.class_id order by i.confidence desc nulls last) filter (where i.class_id is not null))[1],
         (array_agg(i.classification_method order by i.confidence desc nulls last))[1],
         max(i.confidence)
    into v_review, v_buyer_id, i_class, i_method, i_conf
  from public.product_items i
  where i.publication_number = n.publication_number and i.side = 'demand';

  if v_family is null then
    v_class := i_class;
    v_method := i_method;
    v_conf := i_conf;
    if v_class is null then
      select f.id into v_class
      from unnest(n.cpv_codes) as cpv(code)
      join public.product_family_crosswalk cw on cw.scheme = 'cpv' and left(cw.code, 4) = left(cpv.code, 4)
      join public.product_families f on f.id = cw.family_id and f.level = 1 and f.in_focus
      order by f.sort_order, f.id
      limit 1;
      if v_class is not null then
        v_method := 'cpv_prefix';
      end if;
    end if;
  end if;

  if v_family is not null and v_method in ('rules', 'llm') then
    w := w || format('family %s assigned by %s (confidence %s)', v_family, v_method,
                     coalesce(round(v_conf::numeric, 2)::text, 'unknown'));
  end if;
  if coalesce(v_review, false) then
    w := w || 'tender classification needs review'::text;
  end if;
  if v_family is null then
    w := w || case when v_class is not null
                   then format('tender not classified to a family yet: class %s used (%s)', v_class, coalesce(v_method, 'unknown method'))
                   else 'tender not classified: family and class unknown' end;
  end if;
  if cardinality(v_calibres) > 1 then
    w := w || format('tender lists %s calibres (%s): scope uses %s', cardinality(v_calibres),
                     array_to_string(v_calibres, ', '), v_calibres[1]);
  end if;
  if n.estimated_value is null then
    w := w || 'estimated value not published (unknown)'::text;
  end if;
  v_latest := greatest(n.deadline_tender, n.deadline_request);
  if v_latest is null then
    w := w || 'deadline not published (unknown)'::text;
  elsif v_latest < now() then
    w := w || 'tender deadline has passed'::text;
  end if;

  return jsonb_build_object(
    'tender', jsonb_build_object(
      'ref', 'ted:' || n.publication_number, 'layer', 'ted_live', 'publication_number', n.publication_number,
      'title', left(coalesce(n.title_en, n.title_original), 300), 'title_original', left(n.title_original, 300),
      'description', left(n.description, 300),
      'buyer', n.buyer_name, 'buyer_id', v_buyer_id, 'buyer_identifier', n.buyer_identifier,
      'country', n.buyer_country, 'buyer_legal_type', n.buyer_legal_type, 'main_activity', n.main_activity,
      'notice_type', n.notice_type, 'procedure_type', n.procedure_type, 'legal_basis', n.legal_basis,
      'publication_date', n.publication_date, 'cpv_codes', to_jsonb(n.cpv_codes[1:v_lim]),
      'families', v_fams, 'family_id', v_family, 'class_id', v_class, 'calibre', v_calibres[1],
      'classification_method', v_method, 'needs_review', coalesce(v_review, false),
      'next_deadline', coalesce(n.deadline_tender, n.deadline_request),
      'deadline_kind', case when n.deadline_tender is not null then 'tenders'
                            when n.deadline_request is not null then 'requests_to_participate' end,
      'is_open', v_latest >= now(),
      'estimated_value', n.estimated_value, 'currency', n.estimated_currency,
      'value_source', n.estimated_value_source, 'ted_url', n.ted_url),
    'scope', jsonb_build_object(
      'family_id', v_family, 'class_id', v_class, 'calibre', v_calibres[1],
      'country', upper(nullif(btrim(n.buyer_country), '')), 'buyer_id', v_buyer_id, 'buyer_name', n.buyer_name,
      'buyer_identifier', n.buyer_identifier, 'buyer_legal_type', n.buyer_legal_type),
    'warnings', to_jsonb(w));
end;
$$;

-- ------------------------------------------------------------------------------------------ companies

-- Supplier facts: overview, due diligence, KYC, deal baseline, proof in the scope family (or class).
create or replace function public.agent_ctx_supplier(p_supplier_id text, p_family_id text default null,
                                                     p_calibre text default null, p_limit int default 8)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  o          record;
  d          record;
  b          record;
  v_lim      int := greatest(1, least(coalesce(p_limit, 8), 25));
  v_class    text;
  v_is_class boolean := false;
  v_fams     jsonb;
  v_scope    jsonb;
  v_kyc      jsonb;
  v_kyc_open jsonb;
begin
  select * into o from public.supplier_overview s where s.id = p_supplier_id;
  if not found then
    return null;
  end if;
  if p_family_id is not null then
    select coalesce(f.parent_id, f.id), f.parent_id is null into v_class, v_is_class
    from public.product_families f where f.id = p_family_id;
  end if;

  with sf as (
    select u.* from public.ui_supplier_families u where u.supplier_id = p_supplier_id
  ),
  sc as (
    select * from sf
    where p_family_id is not null
      and (sf.family_id = p_family_id or (coalesce(v_is_class, false) and sf.class_id = v_class))
  )
  select
    (select coalesce(jsonb_agg(jsonb_build_object(
              'family_id', x.family_id, 'family_name', x.family_name, 'proven_lots', x.proven_lots,
              'declared_items', x.declared_items, 'calibres', to_jsonb(x.calibres),
              'best_evidence', x.best_evidence, 'last_award', x.last_award)
            order by x.o), '[]'::jsonb)
     from (select sf.*, row_number() over (order by (sf.family_id = p_family_id) desc nulls last,
                                           sf.proven_lots desc, sf.declared_items desc, sf.family_id) as o
           from sf) x
     where x.o <= v_lim),
    (select case when p_family_id is not null then jsonb_build_object(
              'family_id', p_family_id,
              'proven_lots', coalesce(sum(sc.proven_lots), 0),
              'declared_items', coalesce(sum(sc.declared_items), 0),
              'calibres', coalesce((select jsonb_agg(distinct c) from sc s2, unnest(s2.calibres) c), '[]'::jsonb),
              'calibre_match', case when p_calibre is null then null
                                    else coalesce(bool_or(p_calibre = any (sc.calibres)), false) end,
              'best_evidence', (array_agg(sc.best_evidence order by case sc.best_evidence
                                  when 'independently_verified' then 0 when 'document_verified' then 1
                                  when 'peer_attested' then 2 when 'declared' then 3 when 'inferred' then 4 else 5 end)
                                filter (where sc.best_evidence is not null))[1],
              'last_award', max(sc.last_award)) end
     from sc)
    into v_fams, v_scope;

  select dd.red_flags, dd.caps_applied, dd.gates, dd.data_coverage, dd.method_version
    into d
  from public.supplier_due_diligence dd where dd.supplier_id = p_supplier_id;

  select coalesce(jsonb_object_agg(k.check_code, k.status), '{}'::jsonb),
         coalesce(jsonb_agg(jsonb_build_object('code', k.check_code, 'title', k.title, 'gate', k.gate,
                                               'status', k.status, 'summary', left(k.summary, 300))
                            order by k.check_code) filter (where k.status not in ('pass', 'info')), '[]'::jsonb)
    into v_kyc, v_kyc_open
  from public.supplier_kyc_checks k where k.supplier_id = p_supplier_id;

  select db.largest_lot_eur, db.revenue_eur, db.revenue_year, db.single_bid_share, db.trader_not_manufacturer,
         db.related_supplier_ids, db.baseline
    into b
  from public.supplier_deal_baseline db where db.supplier_id = p_supplier_id;

  return jsonb_build_object(
    'ref', 'supplier:' || o.id, 'layer', 'platform', 'supplier_id', o.id, 'name', o.name, 'legal_name', o.legal_name,
    'country', o.country, 'city', o.city, 'company_size', o.company_size,
    'dd_score', o.total_score, 'kyc_score', o.total_score, 'verdict', o.verdict, 'dd_confidence', o.confidence,
    'data_coverage', d.data_coverage, 'method_version', d.method_version, 'assessed_at', o.assessed_at,
    'scores', jsonb_build_object('identity', o.identity_score, 'ownership', o.ownership_score,
                                 'track_record', o.track_record_score, 'financial', o.financial_score,
                                 'defence_credentials', o.defence_credentials_score, 'integrity', o.integrity_score),
    'red_flag_count', o.red_flag_count,
    'red_flags', coalesce((select jsonb_agg(left(r, 300)) from unnest(d.red_flags[1:v_lim]) r), '[]'::jsonb),
    'caps_applied', coalesce(to_jsonb(d.caps_applied[1:v_lim]), '[]'::jsonb),
    'gates', coalesce((select jsonb_agg(jsonb_build_object('check', g ->> 'check', 'status', g ->> 'status'))
                       from jsonb_array_elements(public.agent_ctx_slice(d.gates, v_lim)) g), '[]'::jsonb),
    'kyc_status', o.kyc_status, 'kyc_refresh_due', o.refresh_due, 'open_kyc_items', o.open_kyc_items,
    'kyc_checks', v_kyc, 'kyc_open', public.agent_ctx_slice(v_kyc_open, v_lim),
    'deal_baseline', case when b is null then null else jsonb_build_object(
        'largest_lot_eur', b.largest_lot_eur, 'revenue_eur', b.revenue_eur, 'revenue_year', b.revenue_year,
        'single_bid_share', b.single_bid_share, 'trader_not_manufacturer', b.trader_not_manufacturer,
        'related_supplier_ids', to_jsonb(b.related_supplier_ids),
        'lot_values_by_cpv', b.baseline -> 'D9_price_baseline_by_cpv',
        'note', b.baseline ->> 'D9_note') end,
    'in_scope', v_scope,
    'families', v_fams);
end;
$$;

-- Buyer facts: profile (when the buyer is in buyers) plus its TED history.
create or replace function public.agent_ctx_buyer(p_buyer_id text, p_name text default null, p_identifier text default null,
                                                  p_publication_number text default null, p_limit int default 8)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  b        public.buyers%rowtype;
  v_found  boolean := false;
  v_lim    int := greatest(1, least(coalesce(p_limit, 8), 25));
  v_name   text := nullif(btrim(p_name), '');
  v_ident  text := nullif(btrim(p_identifier), '');
  v_pubs   text[];
  v_hist   jsonb;
  v_fams   jsonb;
  v_procs  jsonb;
  v_wins   jsonb;
  v_country text;
begin
  if p_buyer_id is not null then
    select * into b from public.buyers x where x.id = p_buyer_id;
    v_found := found;
    if v_found then
      v_name := coalesce(v_name, b.name);
      v_ident := coalesce(v_ident, b.identifier);
    end if;
  end if;
  if v_name is null and v_ident is null and not v_found then
    return null;
  end if;

  select coalesce(array_agg(distinct x.pub), '{}'::text[]) into v_pubs
  from (select i.publication_number as pub
        from public.product_items i
        where v_found and i.buyer_id = p_buyer_id and i.side = 'demand' and i.publication_number is not null
        union
        select t.publication_number
        from public.ted_notices t
        where (v_ident is not null and t.buyer_identifier = v_ident)
           or (v_name is not null and lower(btrim(t.buyer_name)) = lower(v_name))
           or t.publication_number = p_publication_number) x;

  select jsonb_build_object(
           'notices', count(*), 'competitions', count(*) filter (where t.kind = 'competition'),
           'results', count(*) filter (where t.kind = 'result'),
           'first_notice', min(t.publication_date), 'last_notice', max(t.publication_date),
           'open_tenders', count(*) filter (where t.kind = 'competition'
                                             and greatest(t.deadline_tender, t.deadline_request) >= now())),
         (array_agg(t.buyer_country order by t.publication_date desc))[1]
    into v_hist, v_country
  from public.ted_notices t where t.publication_number = any (v_pubs);

  select coalesce(jsonb_agg(jsonb_build_object('procedure_type', s.procedure_type, 'tenders', s.n)
                            order by s.n desc, s.procedure_type), '[]'::jsonb)
    into v_procs
  from (select t.procedure_type, count(*)::int as n from public.ted_notices t
        where t.publication_number = any (v_pubs) and t.kind = 'competition' and t.procedure_type is not null
        group by 1 order by 2 desc, 1 limit v_lim) s;

  select coalesce(jsonb_agg(jsonb_build_object('family_id', s.family_id, 'tenders', s.n)
                            order by s.n desc, s.family_id), '[]'::jsonb)
    into v_fams
  from (select u.family_id, count(distinct u.publication_number)::int as n from public.ui_tender_families u
        where u.publication_number = any (v_pubs)
        group by 1 order by 2 desc, 1 limit v_lim) s;

  select coalesce(jsonb_agg(jsonb_build_object('winner', s.winner_name, 'country', s.winner_country, 'awards', s.n)
                            order by s.n desc, s.winner_name), '[]'::jsonb)
    into v_wins
  from (select a.winner_name, a.winner_country, count(*)::int as n from public.ted_awards a
        where a.publication_number = any (v_pubs) and a.winner_name is not null
        group by 1, 2 order by 3 desc, 1 limit v_lim) s;

  return jsonb_build_object(
    'ref', case when v_found then 'buyer:' || b.id when p_publication_number is not null then 'ted:' || p_publication_number end,
    'layer', case when v_found then 'platform' else 'ted_live' end,
    'buyer_id', case when v_found then b.id else p_buyer_id end, 'profiled', v_found,
    'name', v_name, 'legal_name', b.legal_name, 'identifier', v_ident,
    'country', coalesce(b.country, v_country), 'city', b.city, 'legal_type', b.legal_type,
    'main_activity', b.main_activity, 'summary', left(b.summary, 300),
    'ted', v_hist, 'families', v_fams, 'procedure_types', v_procs, 'top_winners', v_wins);
end;
$$;

-- ------------------------------------------------------------------------------------------ evidence

-- kb_search (WS1) wrapper: optional at run time, refs in p_exclude_refs dropped, snippets ≤ 300 chars.
create or replace function public.agent_ctx_evidence(
  p_query_embedding extensions.vector(768), p_query_text text, p_kinds text[], p_family_id text, p_calibre text,
  p_cpv_prefix text, p_country text, p_viewer text, p_limit int, p_exclude_refs text[] default '{}')
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_lim int := greatest(1, least(coalesce(p_limit, 8), 25));
  v     jsonb;
begin
  if to_regprocedure('public.kb_search(extensions.vector, text, text[], text, text, text, text, text, integer)') is null then
    return jsonb_build_object('items', '[]'::jsonb, 'warning', 'kb_search() not available yet: evidence empty');
  end if;
  begin
    v := public.kb_search(p_query_embedding, p_query_text, p_kinds, p_family_id, p_calibre, p_cpv_prefix, p_country,
                          p_viewer, v_lim + coalesce(cardinality(p_exclude_refs), 0));
  exception
    when undefined_function or undefined_table or undefined_column then
      return jsonb_build_object('items', '[]'::jsonb, 'warning', 'kb_search() not available: ' || sqlerrm);
    when others then
      return jsonb_build_object('items', '[]'::jsonb, 'warning', 'kb_search() failed: ' || sqlerrm);
  end;
  select coalesce(jsonb_agg(t.item || jsonb_build_object('snippet', left(t.item ->> 'snippet', 300),
                                                          'layer', t.item -> 'layer')
                            order by t.o), '[]'::jsonb)
    into v
  from (select e.item, e.o
        from jsonb_array_elements(case when jsonb_typeof(v) = 'array' then v else '[]'::jsonb end)
             with ordinality as e(item, o)
        where coalesce(e.item ->> 'ref', '') <> ''
          and not (e.item ->> 'ref' = any (coalesce(p_exclude_refs, '{}'::text[])))
        order by e.o
        limit v_lim) t;
  return jsonb_build_object('items', v, 'warning', null);
end;
$$;

-- Recent RAG cards of one company (check_counterparty): contracts/awards of a supplier, tenders/awards of a buyer,
-- and conversations the viewer had with it. Reads kb_chunks (WS1) directly because kb_search has no company filter.
create or replace function public.agent_ctx_company_chunks(p_supplier_id text, p_buyer_id text, p_viewer text,
                                                           p_limit int default 8)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_lim int := greatest(1, least(coalesce(p_limit, 8), 25));
  v     jsonb;
begin
  begin
    select coalesce(jsonb_agg(jsonb_build_object('ref', k.ref, 'kind', k.kind, 'layer', k.layer, 'title', k.title,
                                                 'snippet', left(k.content, 300), 'facts', k.facts, 'score', null)
                              order by k.occurred_at desc nulls last, k.ref), '[]'::jsonb)
      into v
    from (select c.*
          from public.kb_chunks c
          where ((p_supplier_id is not null and c.supplier_id = p_supplier_id and c.kind in ('contract', 'award'))
              or (p_buyer_id is not null and c.buyer_id = p_buyer_id and c.kind in ('tender', 'award'))
              or (c.kind = 'conversation' and p_viewer is not null and p_viewer = any (c.party_ids)
                  and (p_supplier_id = any (c.party_ids) or p_buyer_id = any (c.party_ids))))
            and (c.visibility = 'public' or (p_viewer is not null and p_viewer = any (c.party_ids)))
          order by c.occurred_at desc nulls last, c.ref
          limit v_lim) k;
  exception when undefined_table or undefined_column then
    return jsonb_build_object('items', '[]'::jsonb, 'warning', 'kb_chunks not available yet: evidence empty');
  end;
  return jsonb_build_object('items', v, 'warning', null);
end;
$$;

-- ------------------------------------------------------------------------------------------ price anchors

-- Same family (and the same calibre when known) only, never a sibling family (KB 27 §4f, §6.5):
--   contract        awarded lot value of a profiled supplier (basis awarded_lot_value)
--   catalogue_item  unit price a supplier derived from a TED lot with a stated quantity (catalogue_derived_unit_price)
--   award           TED notice total over all lots and winners: an upper bound only (award_notice_total_upper_bound)
-- Interleaved contracts : catalogue : awards ≈ 3 : 2 : 1, newest first. Layer taken from kb_chunks when present.
create or replace function public.agent_ctx_anchors(p_family_id text, p_calibre text default null, p_limit int default 8)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_lim     int := greatest(1, least(coalesce(p_limit, 8), 25));
  v_items   jsonb;
  w         text[] := '{}';
  v_n       int;
  v_unknown int;
  v_awards  int;
  v_curs    int;
  v_units   int;
begin
  if p_family_id is null
     or not exists (select 1 from public.product_families f where f.id = p_family_id and f.parent_id is not null) then
    return jsonb_build_object('items', '[]'::jsonb, 'warnings', jsonb_build_array(
      case when p_family_id is null then 'no price anchors: family unknown'
           else format('no price anchors: %s is not a product family (anchors need the same family)', p_family_id) end));
  end if;

  with src as (
    select i.source_table, i.source_ref, i.supplier_id, i.org_ref, i.publication_number, i.lot_id,
           nullif(btrim(i.attrs ->> 'calibre'), '') as calibre, i.observed_at, i.extra
    from public.product_items i
    where i.family_id = p_family_id
      and i.side = 'supply'
      and i.source_table in ('supplier_contracts', 'ted_awards', 'suppliers.catalogue')
      and (p_calibre is null or nullif(btrim(i.attrs ->> 'calibre'), '') = p_calibre)
  ),
  a as (
    select 1 as prio, s.observed_at, s.source_ref as k, jsonb_build_object(
             'ref', s.source_ref, 'kind', 'contract', 'layer', 'ted_live', 'family_id', p_family_id, 'calibre', s.calibre,
             'value', public.agent_ctx_num(s.extra -> 'contract' -> 'awarded_value'),
             'currency', s.extra -> 'contract' ->> 'awarded_currency',
             'quantity', s.extra -> 'contract' ->> 'quantity', 'unit', null, 'unit_price', null,
             'basis', 'awarded_lot_value', 'supplier_id', s.supplier_id,
             'publication_number', s.publication_number, 'lot_id', s.lot_id,
             'buyer', s.extra -> 'contract' ->> 'buyer', 'buyer_country', s.extra -> 'contract' ->> 'buyer_country',
             'date', s.observed_at::date,
             'framework_agreement', (s.extra -> 'contract' ->> 'framework_agreement')::boolean,
             'tenders_received', public.agent_ctx_num(s.extra -> 'contract' -> 'tenders_received'),
             'ted_url', s.extra -> 'contract' ->> 'ted_url') as item
    from src s
    where s.source_table = 'supplier_contracts'
    union all
    select 2, s.observed_at, s.source_ref, jsonb_build_object(
             'ref', s.source_ref, 'kind', 'catalogue_item', 'layer', 'platform', 'family_id', p_family_id,
             'calibre', s.calibre, 'value', null, 'currency', r.m[1], 'quantity', null, 'unit', r.m[3],
             'unit_price', replace(r.m[2], ',', '')::numeric, 'basis', 'catalogue_derived_unit_price',
             'note', left(s.extra -> 'catalogue' -> 'commercial' ->> 'derived_unit_price', 300),
             'supplier_id', s.supplier_id, 'date', s.observed_at::date)
    from src s
    cross join lateral (
      select regexp_match(s.extra -> 'catalogue' -> 'commercial' ->> 'derived_unit_price',
                          '^\s*([A-Z]{3})\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s+per\s+([^\s,;()]+)') as m) r
    where s.source_table = 'suppliers.catalogue'
      and nullif(btrim(s.extra -> 'catalogue' -> 'commercial' ->> 'derived_unit_price'), '') is not null
    union all
    select 3, s.observed_at, s.source_ref, jsonb_build_object(
             'ref', 'award:' || substr(s.source_ref, 5), 'kind', 'award', 'layer', 'ted_live', 'family_id', p_family_id,
             'calibre', s.calibre,
             'value', public.agent_ctx_num(s.extra -> 'award' -> 'notice_value'),
             'currency', s.extra -> 'award' ->> 'currency', 'quantity', null, 'unit', null, 'unit_price', null,
             'basis', 'award_notice_total_upper_bound', 'winner', s.org_ref, 'supplier_id', s.supplier_id,
             'publication_number', s.publication_number,
             'co_winners', public.agent_ctx_num(s.extra -> 'award' -> 'co_winners'),
             'buyer', s.extra -> 'award' ->> 'buyer_name', 'buyer_country', s.extra -> 'award' ->> 'buyer_country',
             'date', s.observed_at::date)
    from src s
    where s.source_table = 'ted_awards' and s.source_ref like 'ted:%'
  ),
  ranked as (
    select a.*, row_number() over (partition by a.prio order by a.observed_at desc nulls last, a.k) as rn
    from a
  )
  select coalesce(jsonb_agg(r.item order by r.rn * case r.prio when 1 then 1.0 when 2 then 1.5 else 3.0 end, r.prio, r.rn),
                  '[]'::jsonb)
    into v_items
  from (select * from ranked
        order by rn * case prio when 1 then 1.0 when 2 then 1.5 else 3.0 end, prio, rn
        limit v_lim) r;

  -- layer as stored in the RAG (baseline vs ted_live vs platform), when kb_chunks exists
  begin
    select coalesce(jsonb_agg(x.item || coalesce(
             (select jsonb_build_object('layer', k.layer) from public.kb_chunks k
              where k.kind = x.item ->> 'kind' and k.source_key = x.item ->> 'ref'
              order by (k.layer = 'baseline') limit 1), '{}'::jsonb)
           order by x.o), '[]'::jsonb)
      into v_items
    from jsonb_array_elements(v_items) with ordinality as x(item, o);
  exception when undefined_table or undefined_column then
    null;
  end;

  select count(*)::int,
         (count(*) filter (where x ->> 'value' is null and x ->> 'unit_price' is null))::int,
         (count(*) filter (where x ->> 'kind' = 'award'))::int,
         (count(distinct x ->> 'currency'))::int,
         (count(*) filter (where x ->> 'unit_price' is not null))::int
    into v_n, v_unknown, v_awards, v_curs, v_units
  from jsonb_array_elements(v_items) x;

  if v_n = 0 then
    w := w || format('no price anchors for %s%s', p_family_id, coalesce(' ' || p_calibre, ''));
  else
    if v_unknown > 0 then
      w := w || format('value unknown for %s of %s price anchors', v_unknown, v_n);
    end if;
    if v_awards > 0 then
      w := w || 'award notice_value is the notice total (all lots and winners), not a lot price: upper bound only'::text;
    end if;
    if v_curs > 1 then
      w := w || 'price anchors are in several currencies (not converted)'::text;
    end if;
    if v_units = 0 then
      w := w || 'no unit prices among the anchors: TED lot values carry no quantity (unit price unknown)'::text;
    end if;
  end if;
  if p_calibre is null then
    w := w || 'calibre unknown: price anchors span all calibres of the family'::text;
  end if;
  return jsonb_build_object('items', v_items, 'warnings', to_jsonb(w));
end;
$$;

-- ------------------------------------------------------------------------------------------ regulatory

-- Compact ui_regulatory_profile(family, calibre): defaults from the ontology, never a licence decision.
create or replace function public.agent_ctx_regulatory(p_family_id text, p_calibre text default null, p_limit int default 8)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_lim int := greatest(1, least(coalesce(p_limit, 8), 25));
  r     jsonb;
  v_ml  jsonb;
  v_adr jsonb;
begin
  if p_family_id is null then
    return '{}'::jsonb;
  end if;
  r := public.ui_regulatory_profile(p_family_id, p_calibre);
  if r is null then
    return '{}'::jsonb;
  end if;
  select x into v_ml from jsonb_array_elements(r -> 'eu_ml') x where x ->> 'code' <> 'none' limit 1;
  v_adr := r -> 'adr' -> 0;
  return jsonb_build_object(
    'ref', 'regulatory:' || p_family_id || coalesce('/' || (r ->> 'calibre'), ''),
    'family_id', r ->> 'family_id', 'class_id', r ->> 'class_id', 'calibre', r ->> 'calibre',
    'eu_ml', coalesce(r -> 'derived' -> 'ml_by_calibre' ->> 'code', v_ml ->> 'code'),
    'eu_ml_basis', left(coalesce(r -> 'derived' -> 'ml_by_calibre' ->> 'basis', v_ml ->> 'note'), 300),
    'fsc', coalesce(r -> 'derived' -> 'fsc_by_bore' ->> 'code', r -> 'fsc' -> 0 ->> 'code'),
    'hs', coalesce((select jsonb_agg(x ->> 'code') from jsonb_array_elements(public.agent_ctx_slice(r -> 'hs', v_lim)) x), '[]'::jsonb),
    'stanag', coalesce((select jsonb_agg(x ->> 'code') from jsonb_array_elements(public.agent_ctx_slice(r -> 'stanag', v_lim)) x), '[]'::jsonb),
    'fd_2021_555', coalesce((select jsonb_agg(x ->> 'code') from jsonb_array_elements(public.agent_ctx_slice(r -> 'fd_2021_555', v_lim)) x), '[]'::jsonb),
    'adr', coalesce((r -> 'derived' -> 'adr_typical' ->> 'un_number') || ' ' || (r -> 'derived' -> 'adr_typical' ->> 'classification'),
                    v_adr ->> 'code'),
    'adr_small_arms', r -> 'derived' -> 'adr_small_arms',
    'verified', coalesce((v_ml ->> 'verified')::boolean, false)
                and coalesce(r -> 'derived' -> 'ml_by_calibre', 'null'::jsonb) = 'null'::jsonb
                and coalesce((v_adr ->> 'verified')::boolean, true),
    'notes', coalesce((select jsonb_agg(left(x #>> '{}', 300)) from jsonb_array_elements(public.agent_ctx_slice(r -> 'notes', v_lim)) x), '[]'::jsonb),
    'note', 'defaults from the ontology, not a licence decision; the per-deal checks D2/D3/D8 decide');
end;
$$;

-- ------------------------------------------------------------------------------------------ deal checks, mandates

-- Latest deal_checks row (D1-D14) for tender × supplier; optional until deal_due_diligence.py --push has run.
create or replace function public.agent_ctx_deal_check(p_publication_number text, p_supplier_id text, p_limit int default 8)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_lim int := greatest(1, least(coalesce(p_limit, 8), 25));
  v     jsonb;
begin
  if p_publication_number is null or p_supplier_id is null then
    return jsonb_build_object('check', null, 'warning', null);
  end if;
  begin
    select jsonb_build_object(
             'ref', 'deal:' || d.deal_id, 'layer', 'platform', 'deal_id', d.deal_id, 'decision', d.decision,
             'method_version', d.method_version, 'checked_at', d.checked_at,
             'blocking', coalesce((select jsonb_agg(left(x, 300)) from unnest(d.blocking[1:v_lim]) x), '[]'::jsonb),
             'conditions', coalesce((select jsonb_agg(left(x, 300)) from unnest(d.conditions[1:v_lim]) x), '[]'::jsonb),
             'checks', coalesce((select jsonb_object_agg(i.code, i.status) from public.deal_check_items i
                                 where i.deal_id = d.deal_id), '{}'::jsonb),
             'open_checks', coalesce((select jsonb_agg(jsonb_build_object('code', i.code, 'title', i.title,
                                                                          'status', i.status, 'summary', left(i.summary, 300))
                                                       order by substr(i.code, 2)::int)
                                      from (select * from public.deal_check_items i2
                                            where i2.deal_id = d.deal_id and i2.status in ('condition', 'escalate', 'fail')
                                            order by substr(i2.code, 2)::int limit v_lim) i), '[]'::jsonb))
      into v
    from public.deal_checks d
    where d.deal_id = p_publication_number || ':' || p_supplier_id;
  exception when undefined_table or undefined_column then
    return jsonb_build_object('check', null,
      'warning', 'deal_checks not available yet (run deal_due_diligence.py --push): facts.deal_checks unknown');
  end;
  return jsonb_build_object('check', v,
    'warning', case when v is null then format('no deal check for %s × %s yet: facts.deal_checks unknown',
                                               p_publication_number, p_supplier_id) end);
end;
$$;

-- The viewer's own agent mandate (K11): first active agent_mandates row among the candidate company ids, else the
-- default platform policy of deal_due_diligence.py. Never returns the other party's mandate.
create or replace function public.agent_ctx_mandate(p_company_ids text[])
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_ids text[] := array_remove(coalesce(p_company_ids, '{}'::text[]), null);
  v     jsonb;
begin
  begin
    select jsonb_build_object(
             'ref', 'mandate:' || m.company_id, 'company_id', m.company_id, 'source', 'signed agent mandate',
             'max_autonomous_value_eur', m.max_autonomous_value_eur, 'min_supplier_score', m.min_supplier_score,
             'allowed_kyc_status', to_jsonb(m.allowed_kyc_status), 'allowed_cpv_prefixes', to_jsonb(m.allowed_cpv_prefixes),
             'escalation_contact', m.escalation_contact, 'signed_by', m.signed_by, 'signed_at', m.signed_at)
      into v
    from public.agent_mandates m
    where m.active and m.company_id = any (v_ids)
    order by array_position(v_ids, m.company_id)
    limit 1;
  exception when undefined_table or undefined_column then
    v := null;
  end;
  return coalesce(v, jsonb_build_object(
    'ref', 'mandate:default', 'company_id', v_ids[1], 'source', 'default platform policy (no signed mandate yet)',
    'max_autonomous_value_eur', 250000, 'min_supplier_score', 6.0,
    'allowed_kyc_status', jsonb_build_array('verified', 'limited'),
    'allowed_cpv_prefixes', jsonb_build_array('3531', '3532', '3533', '3534')));
end;
$$;

-- ------------------------------------------------------------------------------------------ priors

-- priors = baseline (active kb_snapshots row → kb_baseline_stats, falling back family+calibre+country →
-- family+country → family → class; levels with n ≥ 3 first) + platform (closed, non-demo conversations with a
-- deal_outcomes unit price, same family and calibre) + blend w = n_platform / (n_platform + 10).
create or replace function public.agent_ctx_priors(p_family_id text, p_class_id text, p_calibre text, p_country text,
                                                   p_limit int default 8)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_lim   int := greatest(1, least(coalesce(p_limit, 8), 25));
  v_scope text := coalesce(p_family_id, p_class_id);
  v_snap  text;
  v_b     jsonb;
  v_base  jsonb;
  v_cur   text;
  v_bp50  numeric;
  v_n     int;
  v_acc   int;
  v_demo  int;
  v_nu    int;
  v_pp50  numeric;
  v_pcur  text;
  v_w     numeric;
  v_blend numeric;
  v_basis text;
  w       text[] := '{}';
begin
  begin
    select s.id into v_snap from public.kb_snapshots s where s.active order by s.id limit 1;
    if v_snap is null then
      w := w || 'no active baseline snapshot yet: priors.baseline unknown'::text;
    else
      select to_jsonb(b) into v_b
      from public.kb_baseline_stats b
      where b.snapshot_id = v_snap
        and coalesce(b.n_procedures, 0) > 0
        and ((p_family_id is not null and b.scope_level = 'family_calibre_country' and b.family_id = p_family_id
              and b.calibre = p_calibre and b.country = p_country)
          or (p_family_id is not null and b.scope_level = 'family_country' and b.family_id = p_family_id
              and b.country = p_country)
          or (p_family_id is not null and b.scope_level = 'family' and b.family_id = p_family_id)
          or (p_class_id is not null and b.scope_level = 'class' and b.class_id = p_class_id))
      order by (b.n_procedures >= 3) desc,
               case b.scope_level when 'family_calibre_country' then 0 when 'family_country' then 1
                                  when 'family' then 2 else 3 end,
               (b.buyer_legal_type is null) desc, b.n_procedures desc
      limit 1;
      if v_b is null then
        w := w || format('baseline %s has no statistics for %s: priors.baseline unknown', v_snap, coalesce(v_scope, 'this scope'));
      end if;
    end if;
  exception when undefined_table or undefined_column then
    w := w || 'baseline tables not available yet: priors.baseline unknown'::text;
  end;

  if v_b is not null then
    v_cur := upper(v_b ->> 'currency');
    v_bp50 := (v_b ->> 'unit_price_p50')::numeric;
    v_base := jsonb_build_object(
      'ref', format('baseline:%s/%s/%s', v_snap, v_b ->> 'scope_level',
                    concat_ws('/', coalesce(v_b ->> 'family_id', v_b ->> 'class_id'), v_b ->> 'calibre', v_b ->> 'country')),
      'layer', 'baseline', 'snapshot_id', v_snap, 'scope_level', v_b ->> 'scope_level',
      'family_id', v_b ->> 'family_id', 'class_id', v_b ->> 'class_id', 'calibre', v_b ->> 'calibre',
      'country', v_b ->> 'country', 'buyer_legal_type', v_b ->> 'buyer_legal_type',
      'n', (v_b ->> 'n_procedures')::int, 'n_awarded', (v_b ->> 'n_awarded')::int,
      'n_unit_prices', (v_b ->> 'n_unit_prices')::int,
      'unit_price_p25', v_b -> 'unit_price_p25', 'unit_price_p50', v_b -> 'unit_price_p50',
      'unit_price_p75', v_b -> 'unit_price_p75', 'currency', v_b -> 'currency',
      'quantity_p50', v_b -> 'quantity_p50', 'quantity_unit', v_b -> 'quantity_unit',
      'awarded_value_p50', v_b -> 'awarded_value_p50', 'bids_median', v_b -> 'bids_p50',
      'value_published_share', v_b -> 'value_published_share', 'top_winner_share', v_b -> 'top_winner_share',
      'top_winners', public.agent_ctx_slice(v_b -> 'top_winners', v_lim),
      'procedure_types', v_b -> 'procedure_types', 'days_to_award_p50', v_b -> 'days_to_award_p50',
      'repeat_interval_days_p50', v_b -> 'repeat_interval_days_p50',
      'example_refs', public.agent_ctx_slice(v_b -> 'example_refs', v_lim));
    if v_bp50 is null then
      w := w || 'baseline unit price unknown in this scope (no stated quantities)'::text;
    end if;
  end if;

  -- platform: our own closed conversations (demo conversations are fixtures and are excluded)
  with c as (
    select cv.id, cv.demo, cv.outcome ->> 'final_status' as final_status, o.unit_price, upper(o.currency) as currency
    from public.agent_conversations cv
    left join public.deal_outcomes o on o.conversation_id = cv.id
    where cv.status = 'closed'
      and v_scope is not null
      and cv.family_id = v_scope
      and (p_calibre is null or cv.calibre is null or cv.calibre = p_calibre)
  )
  select (count(*) filter (where not c.demo))::int,
         (count(*) filter (where not c.demo and c.final_status = 'accepted'))::int,
         (count(*) filter (where c.demo))::int,
         mode() within group (order by c.currency) filter (where not c.demo and c.unit_price is not null)
    into v_n, v_acc, v_demo, v_pcur
  from c;
  v_pcur := coalesce(v_cur, v_pcur);

  select count(*)::int, percentile_cont(0.5) within group (order by o.unit_price)
    into v_nu, v_pp50
  from public.agent_conversations cv
  join public.deal_outcomes o on o.conversation_id = cv.id
  where cv.status = 'closed' and not cv.demo
    and v_scope is not null and cv.family_id = v_scope
    and (p_calibre is null or cv.calibre is null or cv.calibre = p_calibre)
    and o.unit_price is not null and upper(o.currency) = v_pcur;

  if v_demo > 0 then
    w := w || format('%s demo conversation(s) excluded from platform priors', v_demo);
  end if;

  v_w := round(v_nu::numeric / (v_nu + 10), 4);
  if v_bp50 is not null and v_pp50 is not null then
    v_blend := round((1 - v_w) * v_bp50 + v_w * v_pp50::numeric, 4);
    v_basis := 'blend';
  elsif v_bp50 is not null then
    v_blend := v_bp50;
    v_basis := 'baseline_only';
  elsif v_pp50 is not null then
    v_blend := round(v_pp50::numeric, 4);
    v_basis := 'platform_only';
    w := w || 'no baseline prior: blended unit price uses platform outcomes only'::text;
  end if;

  return jsonb_build_object(
    'priors', jsonb_build_object(
      'scope', jsonb_build_object('family_id', p_family_id, 'class_id', p_class_id, 'calibre', p_calibre, 'country', p_country),
      'baseline', v_base,
      'platform', jsonb_build_object(
        'layer', 'platform', 'n', v_nu, 'n_closed', v_n, 'n_accepted', v_acc,
        'unit_price_p50', round(v_pp50::numeric, 4), 'currency', v_pcur,
        'unit_price_vs_baseline_p50', case when v_bp50 is not null and v_bp50 <> 0 and v_pp50 is not null
                                           then round(v_pp50::numeric / v_bp50 - 1, 4) end,
        'demo_excluded', v_demo, 'scope', 'family + calibre; country not filtered'),
      'blend', jsonb_build_object('weight_platform', v_w, 'unit_price_p50', v_blend, 'currency', v_pcur,
                                  'basis', v_basis, 'rule', 'w = n_platform / (n_platform + 10)')),
    'warnings', to_jsonb(w));
end;
$$;

-- ------------------------------------------------------------------------------------------ grants

revoke all on function public.agent_ctx_num(jsonb) from public, anon, authenticated;
revoke all on function public.agent_ctx_slice(jsonb, int) from public, anon, authenticated;
revoke all on function public.agent_ctx_tender(text, int) from public, anon, authenticated;
revoke all on function public.agent_ctx_supplier(text, text, text, int) from public, anon, authenticated;
revoke all on function public.agent_ctx_buyer(text, text, text, text, int) from public, anon, authenticated;
revoke all on function public.agent_ctx_evidence(extensions.vector, text, text[], text, text, text, text, text, int, text[])
  from public, anon, authenticated;
revoke all on function public.agent_ctx_company_chunks(text, text, text, int) from public, anon, authenticated;
revoke all on function public.agent_ctx_anchors(text, text, int) from public, anon, authenticated;
revoke all on function public.agent_ctx_regulatory(text, text, int) from public, anon, authenticated;
revoke all on function public.agent_ctx_deal_check(text, text, int) from public, anon, authenticated;
revoke all on function public.agent_ctx_mandate(text[]) from public, anon, authenticated;
revoke all on function public.agent_ctx_priors(text, text, text, text, int) from public, anon, authenticated;

grant execute on function public.agent_ctx_num(jsonb) to service_role;
grant execute on function public.agent_ctx_slice(jsonb, int) to service_role;
grant execute on function public.agent_ctx_tender(text, int) to service_role;
grant execute on function public.agent_ctx_supplier(text, text, text, int) to service_role;
grant execute on function public.agent_ctx_buyer(text, text, text, text, int) to service_role;
grant execute on function public.agent_ctx_evidence(extensions.vector, text, text[], text, text, text, text, text, int, text[])
  to service_role;
grant execute on function public.agent_ctx_company_chunks(text, text, text, int) to service_role;
grant execute on function public.agent_ctx_anchors(text, text, int) to service_role;
grant execute on function public.agent_ctx_regulatory(text, text, int) to service_role;
grant execute on function public.agent_ctx_deal_check(text, text, int) to service_role;
grant execute on function public.agent_ctx_mandate(text[]) to service_role;
grant execute on function public.agent_ctx_priors(text, text, text, text, int) to service_role;
