-- Unit-price priors from the strict source only (Bug: POL 155 mm showed 3,951,125,739 PLN "per unit").
--
-- agent_ctx_priors: every unit-price field of priors.baseline (n_unit_prices, unit_price_p25/p50/p75, currency,
--   quantity_p50, quantity_unit) now comes from kb_baseline_unit_prices of the active snapshot, never from
--   kb_baseline_stats (baseline_v1 mixed quantity-1 lump sums and unit-less quantities into its 23 unit-price rows).
--   Counts, bids, outcome rates, winners and timing still come from kb_baseline_stats. Keys are kept; when a unit
--   price is unknown they are null and `warnings` says why. Added keys: unit_price_ref, unit_price_scope_level,
--   unit_price_basis, unit_price_example_refs, value_currency (currency of awarded_value_p50). Blend unchanged:
--   w = n_platform / (n_platform + 10).
-- agent_ctx_anchors: also warns when contract anchors are framework agreements (value = framework ceiling).
-- CREATE OR REPLACE keeps the grants (service_role only).

-- priors = baseline of the active snapshot + platform outcomes + blend w = n_platform / (n_platform + 10).
--
-- Baseline counts, bids, outcome rates, winners and timing: kb_baseline_stats, falling back family+calibre+country →
-- family+country → family → class (levels with n ≥ 3 first). Unchanged.
--
-- Baseline unit prices: ONLY public.kb_baseline_unit_prices (strict: pieces (H87), quantity > 1, single lot with a
-- stated quantity, one result notice). kb_baseline_stats.unit_price_* / quantity_* / n_unit_prices are not read:
-- in baseline_v1 they mix quantity-1 lump sums and quantities without a unit (POL 155 mm ≈ 3.95 bn PLN "per unit").
-- Fallback: family+calibre+country (→ family+calibre, if a snapshot provides that level); only when the subject has
-- no calibre also family+country → family. Never across calibres or at class level: unit prices inside a class or
-- family differ by orders of magnitude (9x19 ≈ 1.35 PLN, a torpedo ≈ 6.4 m PLN). Unknown stays null + warning.
create or replace function public.agent_ctx_priors(p_family_id text, p_class_id text, p_calibre text, p_country text,
                                                   p_limit int default 8)
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
-- Depends on these columns of public.kb_baseline_unit_prices (one row per snapshot × scope × currency; a later
-- snapshot such as baseline_v2 works as soon as its rows are in the view and kb_snapshots.active points to it):
--   snapshot_id, scope_level, family_id, calibre, country, currency, quantity_unit, n,
--   unit_price_p25, unit_price_p50, unit_price_p75, quantity_p50, example_refs, basis
declare
  v_lim   int := greatest(1, least(coalesce(p_limit, 8), 25));
  v_scope text := coalesce(p_family_id, p_class_id);
  v_snap  text;
  v_b     jsonb;
  v_u     jsonb;
  v_ucurs text[];
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

  -- strict unit prices (same active snapshot)
  if v_snap is not null and p_family_id is not null then
    begin
      select to_jsonb(u) into v_u
      from public.kb_baseline_unit_prices u
      where u.snapshot_id = v_snap
        and u.family_id = p_family_id
        and coalesce(u.n, 0) > 0
        and u.unit_price_p50 is not null
        and ((u.scope_level = 'family_calibre_country' and p_calibre is not null
              and u.calibre = p_calibre and u.country = p_country)
          or (u.scope_level = 'family_calibre' and p_calibre is not null and u.calibre = p_calibre)
          or (u.scope_level = 'family_country' and p_calibre is null and u.country = p_country)
          or (u.scope_level = 'family' and p_calibre is null))
      order by case u.scope_level when 'family_calibre_country' then 0 when 'family_calibre' then 1
                                  when 'family_country' then 2 else 3 end,
               u.n desc,
               (upper(u.currency) = upper(v_b ->> 'currency')) desc nulls last,
               u.currency
      limit 1;
      if v_u is not null then
        select array_agg(distinct upper(u.currency) order by upper(u.currency)) into v_ucurs
        from public.kb_baseline_unit_prices u
        where u.snapshot_id = v_snap and u.scope_level = v_u ->> 'scope_level'
          and u.family_id = p_family_id
          and u.calibre is not distinct from (v_u ->> 'calibre')
          and u.country is not distinct from (v_u ->> 'country')
          and upper(u.currency) <> upper(v_u ->> 'currency');
      end if;
    exception when undefined_table or undefined_column then
      w := w || 'kb_baseline_unit_prices not available: baseline unit price unknown'::text;
      v_u := null;
    end;
  end if;

  if v_b is not null or v_u is not null then
    v_cur := upper(coalesce(v_u ->> 'currency', v_b ->> 'currency'));
    v_bp50 := (v_u ->> 'unit_price_p50')::numeric;
    v_base := jsonb_build_object(
      'ref', case when v_b is not null
                  then format('baseline:%s/%s/%s', v_snap, v_b ->> 'scope_level',
                              concat_ws('/', coalesce(v_b ->> 'family_id', v_b ->> 'class_id'), v_b ->> 'calibre', v_b ->> 'country'))
                  else format('baseline:%s/unit_price/%s/%s', v_snap, v_u ->> 'scope_level',
                              concat_ws('/', v_u ->> 'family_id', v_u ->> 'calibre', v_u ->> 'country', v_u ->> 'currency')) end,
      'layer', 'baseline', 'snapshot_id', v_snap, 'scope_level', v_b ->> 'scope_level',
      'family_id', coalesce(v_b ->> 'family_id', v_u ->> 'family_id'), 'class_id', coalesce(v_b ->> 'class_id', p_class_id),
      'calibre', v_b ->> 'calibre', 'country', v_b ->> 'country', 'buyer_legal_type', v_b ->> 'buyer_legal_type',
      'n', (v_b ->> 'n_procedures')::int, 'n_awarded', (v_b ->> 'n_awarded')::int,
      -- unit-price fields: strict source only
      'n_unit_prices', coalesce((v_u ->> 'n')::int, 0),
      'unit_price_p25', v_u -> 'unit_price_p25', 'unit_price_p50', v_u -> 'unit_price_p50',
      'unit_price_p75', v_u -> 'unit_price_p75',
      'currency', to_jsonb(v_cur),
      'quantity_p50', v_u -> 'quantity_p50', 'quantity_unit', v_u -> 'quantity_unit',
      'unit_price_ref', case when v_u is not null then
                          format('baseline:%s/unit_price/%s/%s', v_snap, v_u ->> 'scope_level',
                                 concat_ws('/', v_u ->> 'family_id', v_u ->> 'calibre', v_u ->> 'country', v_u ->> 'currency')) end,
      'unit_price_scope_level', v_u ->> 'scope_level',
      'unit_price_basis', v_u ->> 'basis',
      'unit_price_example_refs', public.agent_ctx_slice(v_u -> 'example_refs', v_lim),
      -- awarded_value_p50 is in the statistics' currency, which can differ from the unit-price currency
      'value_currency', v_b -> 'currency',
      'awarded_value_p50', v_b -> 'awarded_value_p50', 'bids_median', v_b -> 'bids_p50',
      'value_published_share', v_b -> 'value_published_share', 'top_winner_share', v_b -> 'top_winner_share',
      'top_winners', public.agent_ctx_slice(v_b -> 'top_winners', v_lim),
      'procedure_types', v_b -> 'procedure_types', 'days_to_award_p50', v_b -> 'days_to_award_p50',
      'repeat_interval_days_p50', v_b -> 'repeat_interval_days_p50',
      'example_refs', public.agent_ctx_slice(v_b -> 'example_refs', v_lim));

    if v_u is null and p_family_id is null then
      w := w || 'baseline unit price unknown: product family unknown (unit prices are never taken at class level)'::text;
    elsif v_u is null then
      w := w || format('baseline unit price unknown for %s: no unit price with a stated piece quantity > 1 in %s%s',
                       concat_ws(' ', p_family_id, p_calibre, p_country), v_snap,
                       case when p_calibre is not null then ' (not taken from other calibres)' else '' end);
    end if;
    if v_u is null then
      if p_family_id is not null and v_b ->> 'unit_price_p50' is not null then
        w := w || format('%s kb_baseline_stats unit price for this scope not used: it includes lump sums or quantities '
                         'without a unit', v_snap);
      end if;
    else
      if (v_u ->> 'n')::int < 3 then
        w := w || format('baseline unit price rests on %s procedure(s) (%s): weak prior', v_u ->> 'n',
                         array_to_string(public.agent_text_array(v_u -> 'example_refs'), ', '));
      end if;
      if v_u ->> 'scope_level' in ('family_country', 'family') then
        w := w || format('baseline unit price at %s level: calibre unknown, spans all calibres of %s',
                         v_u ->> 'scope_level', p_family_id);
      end if;
      if cardinality(v_ucurs) > 0 then
        w := w || format('baseline unit prices for this scope also exist in %s (not converted; %s shown)',
                         array_to_string(v_ucurs, ', '), v_cur);
      end if;
      if v_b ->> 'currency' is not null and upper(v_b ->> 'currency') <> v_cur then
        w := w || format('baseline unit price in %s, awarded values in %s (value_currency)', v_cur, upper(v_b ->> 'currency'));
      end if;
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
  v_small   int;
  v_fa      int;
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
                          '^\s*[≈~]?\s*([A-Z]{3})\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s+per\s+([^\s,;()]+)') as m) r
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
         (count(*) filter (where x ->> 'unit_price' is not null))::int,
         (count(*) filter (where x ->> 'kind' = 'contract' and (x ->> 'value')::numeric < 100))::int,
         (count(*) filter (where x ->> 'kind' = 'contract' and x ->> 'value' is not null
                             and coalesce((x ->> 'framework_agreement')::boolean, false)))::int
    into v_n, v_unknown, v_awards, v_curs, v_units, v_small, v_fa
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
    if v_small > 0 then
      w := w || format('%s awarded lot value(s) below 100: probably a unit price entered as the lot value (check the notice)', v_small);
    end if;
    if v_fa > 0 then
      w := w || format('%s awarded lot value(s) are framework agreements: the value is usually the framework ceiling '
                       '(maximum, often shared by co-winners), not the amount bought', v_fa);
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
