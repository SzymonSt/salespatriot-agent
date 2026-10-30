-- WS8 baseline_v2, part 2: build functions for an overlay snapshot (kb_snapshots.parent_id).
--
-- * kb_baseline_lots gets quantity_used / quantity_used_unit / quantity_source: the lot's own BT-25 from the result
--   notice XML, else (single-lot procedures only) the BT-25 of the procedure's single-lot notice that baseline_v1 read
--   from the Search API. Never parsed from text. The XML columns stay untouched (they feed the content hash).
-- * kb_baseline_procedures_of(snapshot) / view kb_baseline_procedures_resolved: the snapshot's own procedure rows plus
--   the parent's rows it does not override.
-- * kb_baseline_v2_procedures(snapshot): override rows for every parent procedure with parsed lots (winners paired
--   through the XML, legacy result status, lot bids, single-lot quantity and unit price, family from the lots when
--   the procedure had none). Where a result notice was not parsed, its Search API winners stay.
-- * view kb_baseline_unit_price_points: one strict unit price per lot (lot data) or per procedure (no lot data:
--   the WS8 fix-forward rule of baseline_v1), per resolved snapshot.
-- * kb_baseline_build_stats / kb_baseline_build_chunks rebuilt for overlays; P001/P002 only for the four focus classes.
-- * kb_baseline_inherited(active, kind, source_key, publication_number): is a parent card still visible?
-- * kb_baseline_content_hash / kb_baseline_freeze: lots-based hash and manifest for overlay snapshots; v1 unchanged.

alter table public.kb_baseline_lots add column if not exists quantity_used numeric;
alter table public.kb_baseline_lots add column if not exists quantity_used_unit text;
alter table public.kb_baseline_lots add column if not exists quantity_source text;

-- ---------------------------------------------------------------------------------------------- lot classification
create or replace function public.kb_baseline_lots_classify(p_snapshot text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_status text;
  v_parent text;
  v_n int;
begin
  select s.status, s.parent_id into v_status, v_parent from public.kb_snapshots s where s.id = p_snapshot;
  if v_status is distinct from 'building' then
    raise exception 'kb_baseline_lots_classify: snapshot % is % (must be building)', p_snapshot, coalesce(v_status, 'missing');
  end if;

  update public.kb_baseline_lots l set procedure_id = pr.procedure_id
  from public.kb_baseline_procedures pr
  where l.snapshot_id = p_snapshot and pr.snapshot_id = coalesce(v_parent, p_snapshot)
    and l.publication_number = any (pr.result_publication_numbers);

  with src as (
    select l.publication_number, l.lot_key,
           coalesce(l.n_lots_in_notice, 1) <= 1 as single,
           concat_ws(' ', l.title, l.description) as lot_text,
           concat_ws(' ', pr.title_original, pr.description, pr.title_en) as proc_text,
           concat_ws(' ', pr.title_original, pr.title_en) as proc_title,
           case when cardinality(l.cpv_codes) > 0 then l.cpv_codes else coalesce(pr.cpv_codes, '{}') end as cpv,
           coalesce(pr.cpv_codes, '{}') as proc_cpv
    from public.kb_baseline_lots l
    left join public.kb_baseline_procedures pr on pr.snapshot_id = coalesce(v_parent, p_snapshot) and pr.procedure_id = l.procedure_id
    where l.snapshot_id = p_snapshot
  ),
  r1 as (
    select s.*, public.kb_baseline_classify(case when s.single then concat_ws(' ', s.lot_text, s.proc_text) else s.lot_text end, s.cpv) as c1
    from src s
  ),
  r2 as (
    select r.*,
           case when not r.single and r.c1 ->> 'family_id' is null
                then public.kb_baseline_classify(concat_ws(' ', r.lot_text, r.proc_title),
                                                 (select array_agg(distinct x) from unnest(r.cpv || r.proc_cpv) x)) end as c2
    from r1 r
  ),
  d as (
    select r.publication_number, r.lot_key,
           case when r.c2 ->> 'family_id' is not null then r.c2 else r.c1 end as c,
           case when r.c2 ->> 'family_id' is not null then 'lot+procedure'
                when r.single then 'lot+procedure' else 'lot' end as input,
           r.c1 ->> 'calibre' as lot_calibre, r.c2 is not null and r.c2 ->> 'family_id' is not null as second
    from r2 r
  )
  update public.kb_baseline_lots l set
    class_id = d.c ->> 'class_id',
    family_id = d.c ->> 'family_id',
    calibre = case when d.second then d.lot_calibre else d.c ->> 'calibre' end,
    classification_method = d.c ->> 'method',
    classification_confidence = (d.c ->> 'confidence')::real,
    classification_input = d.input,
    classification_detail = d.c - 'extractor' || jsonb_build_object('extractor', 'rules-v1 (kb_baseline_classify) on lot text')
  from d
  where l.snapshot_id = p_snapshot and l.publication_number = d.publication_number and l.lot_key = d.lot_key;

  -- quantity for the unit price: own BT-25, else the single-lot procedure's BT-25 (v1 Search API field)
  update public.kb_baseline_lots l set
    quantity_used = coalesce(l.quantity, q.quantity),
    quantity_used_unit = case when l.quantity is not null then l.quantity_unit else q.quantity_unit end,
    quantity_source = case when l.quantity is not null then 'result_notice_xml'
                           when q.quantity is not null then 'single_lot_notice_search_api' end
  from (
    select z.publication_number, z.lot_key, pr.quantity, pr.quantity_unit
    from public.kb_baseline_lots z
    left join public.kb_baseline_procedures pr
      on pr.snapshot_id = coalesce(v_parent, p_snapshot) and pr.procedure_id = z.procedure_id
     and coalesce(pr.n_lots, 1) <= 1 and coalesce(z.n_lots_in_notice, 1) <= 1
     and (select count(*) from public.kb_baseline_lots y where y.snapshot_id = z.snapshot_id and y.procedure_id = z.procedure_id) = 1
    where z.snapshot_id = p_snapshot
  ) q
  where l.snapshot_id = p_snapshot and l.publication_number = q.publication_number and l.lot_key = q.lot_key;

  update public.kb_baseline_lots l set
    unit_price_excluded = x.reason,
    unit_price = case when x.reason is null then round(l.value / l.quantity_used, 4) end,
    unit_price_basis = case when x.reason is null then
      'lot value (' || l.value_source || ') / stated quantity (BT-25 ' || l.quantity_used_unit || ', ' || l.quantity_source || ')' end
  from (
    select z.publication_number, z.lot_key,
           case
             when not z.awarded then 'lot not awarded'
             when z.quantity_used is null and z.format <> 'eforms' then 'no structured quantity (pre-eForms notice)'
             when z.quantity_used is null then 'no stated quantity'
             when z.quantity_used_unit is distinct from 'H87' then 'quantity not in pieces (' || coalesce(z.quantity_used_unit, 'no unit') || ')'
             when z.quantity_used <= 1 then 'quantity 1 (lump sum)'
             when z.framework or z.framework_max is not null then 'framework agreement'
             when z.n_winning_tenders <> 1 then 'several winning tenders'
             when z.winner_confirmed is not true then 'winner not confirmed'
             when z.value is null or z.value <= 0 then 'no lot value'
             when z.value_source not in ('tender_value', 'notice_total_one_lot') then 'pre-eForms contract value (framework flag unreliable)'
             when z.currency is null then 'no currency'
           end as reason
    from public.kb_baseline_lots z
    where z.snapshot_id = p_snapshot
  ) x
  where l.snapshot_id = p_snapshot and l.publication_number = x.publication_number and l.lot_key = x.lot_key;
  get diagnostics v_n = row_count;

  return jsonb_build_object(
    'lots', v_n,
    'with_procedure', (select count(*) from public.kb_baseline_lots l where l.snapshot_id = p_snapshot and l.procedure_id is not null),
    'by_class', (select jsonb_object_agg(coalesce(z.class_id, 'undecided'), z.n) from (select l.class_id, count(*) n
                  from public.kb_baseline_lots l where l.snapshot_id = p_snapshot group by 1) z),
    'with_family', (select count(*) from public.kb_baseline_lots l where l.snapshot_id = p_snapshot and l.family_id is not null),
    'with_calibre', (select count(*) from public.kb_baseline_lots l where l.snapshot_id = p_snapshot and l.calibre is not null),
    'with_quantity', (select jsonb_object_agg(z.s, z.n) from (select l.quantity_source s, count(*) n from public.kb_baseline_lots l
                  where l.snapshot_id = p_snapshot and l.quantity_source is not null group by 1) z),
    'with_unit_price', (select count(*) from public.kb_baseline_lots l where l.snapshot_id = p_snapshot and l.unit_price is not null),
    'unit_price_excluded', (select jsonb_object_agg(z.r, z.n) from (select l.unit_price_excluded r, count(*) n
                  from public.kb_baseline_lots l where l.snapshot_id = p_snapshot and l.unit_price_excluded is not null group by 1) z));
end
$$;
revoke all on function public.kb_baseline_lots_classify(text) from public, anon, authenticated;
grant execute on function public.kb_baseline_lots_classify(text) to service_role;

-- ---------------------------------------------------------------------------------------------- resolved procedures
create or replace function public.kb_baseline_procedures_of(p_snapshot text)
returns setof public.kb_baseline_procedures
language sql
stable
security definer
set search_path = ''
as $$
  select p.* from public.kb_baseline_procedures p where p.snapshot_id = p_snapshot
  union all
  select pp.*
  from public.kb_snapshots s
  join public.kb_baseline_procedures pp on pp.snapshot_id = s.parent_id
  where s.id = p_snapshot
    and not exists (select 1 from public.kb_baseline_procedures o where o.snapshot_id = p_snapshot and o.procedure_id = pp.procedure_id)
$$;
revoke all on function public.kb_baseline_procedures_of(text) from public, anon, authenticated;
grant execute on function public.kb_baseline_procedures_of(text) to service_role;

-- Public read: every snapshot's full procedure set (own rows + parent rows it does not override).
create or replace view public.kb_baseline_procedures_resolved with (security_invoker = on) as
select s.id as resolved_snapshot_id, p.*
from public.kb_snapshots s
join public.kb_baseline_procedures p on p.snapshot_id = s.id
union all
select s.id, pp.*
from public.kb_snapshots s
join public.kb_baseline_procedures pp on pp.snapshot_id = s.parent_id
where not exists (select 1 from public.kb_baseline_procedures o where o.snapshot_id = s.id and o.procedure_id = pp.procedure_id);
grant select on public.kb_baseline_procedures_resolved to anon, authenticated, service_role;

-- ---------------------------------------------------------------------------------------------- v2 procedure overrides
create or replace function public.kb_baseline_v2_procedures(p_snapshot text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_status text;
  v_parent text;
  v_n int;
begin
  select s.status, s.parent_id into v_status, v_parent from public.kb_snapshots s where s.id = p_snapshot;
  if v_status is distinct from 'building' or v_parent is null then
    raise exception 'kb_baseline_v2_procedures: snapshot % must be building and have a parent', p_snapshot;
  end if;

  delete from public.kb_baseline_procedures p where p.snapshot_id = p_snapshot;

  insert into public.kb_baseline_procedures (
    snapshot_id, procedure_id, link_method, eforms, tender_publication_number, competition_publication_numbers,
    result_publication_numbers, modification_publication_numbers, outcome, result_status, outcome_basis, overdue,
    legal_basis, procedure_type, buyer_name, buyer_identifier, buyer_country, buyer_legal_type, main_activity,
    title_en, title_original, description, cpv_codes, class_id, family_id, calibre, classification_method,
    classification_confidence, classification_detail, published, deadline, decided, decided_basis, days_to_award,
    estimated_value, estimated_currency, awarded_value, currency, n_lots, quantity, quantity_unit, unit_price,
    bids_received, winners, consortium, non_award_reasons)
  with l as materialized (
    select * from public.kb_baseline_lots x where x.snapshot_id = p_snapshot and x.procedure_id is not null
  ),
  pp as materialized (
    select p.* from public.kb_baseline_procedures p
    where p.snapshot_id = v_parent and p.procedure_id in (select l.procedure_id from l)
  ),
  parsed as (select distinct l.procedure_id, l.publication_number from l),
  -- Search API winners of every result notice (owner nationality, size), by normalised name
  sapi as (
    select pp.procedure_id, r.pub, lower(btrim(w ->> 'name')) as wkey, w as winner
    from pp
    cross join lateral unnest(pp.result_publication_numbers) as r(pub)
    join public.kb_baseline_notices n on n.snapshot_id = v_parent and n.publication_number = r.pub
    cross join lateral jsonb_array_elements(n.winners) w
    where nullif(btrim(w ->> 'name'), '') is not null
  ),
  xw as (                                              -- winners paired through the XML (awarded lots only)
    select l.procedure_id, lower(btrim(w ->> 'name')) as wkey,
           (array_agg(btrim(w ->> 'name') order by l.publication_number, l.lot_key))[1] as name,
           (array_agg(w ->> 'identifier' order by l.publication_number, l.lot_key) filter (where nullif(w ->> 'identifier', '') is not null))[1] as identifier,
           (array_agg(w ->> 'country' order by l.publication_number, l.lot_key) filter (where w ->> 'country' is not null))[1] as country,
           (array_agg(w ->> 'size' order by l.publication_number, l.lot_key) filter (where w ->> 'size' is not null))[1] as size,
           bool_or((w ->> 'sme')::boolean) as sme
    from l cross join lateral jsonb_array_elements(l.winners) w
    where l.awarded and nullif(btrim(w ->> 'name'), '') is not null
    group by 1, 2
  ),
  wall as (
    select x.procedure_id, x.wkey,
           jsonb_strip_nulls(jsonb_build_object('name', x.name, 'identifier', x.identifier, 'country', x.country,
             'size', coalesce(x.size, s.size), 'sme', x.sme, 'owner_nationality', s.own, 'source', 'notice_xml')) as winner
    from xw x
    left join (select sapi.procedure_id, sapi.wkey, max(sapi.winner ->> 'size') as size, max(sapi.winner ->> 'owner_nationality') as own
               from sapi group by 1, 2) s on s.procedure_id = x.procedure_id and s.wkey = x.wkey
    union all                                          -- result notices that were not parsed keep their Search API winners
    select s.procedure_id, s.wkey, s.winner || jsonb_build_object('source', 'search_api')
    from sapi s
    where not exists (select 1 from parsed pa where pa.procedure_id = s.procedure_id and pa.publication_number = s.pub)
      and not exists (select 1 from xw where xw.procedure_id = s.procedure_id and xw.wkey = s.wkey)
  ),
  wagg as (
    select z.procedure_id, jsonb_agg(z.winner order by z.wkey) as winners
    from (select distinct on (w.procedure_id, w.wkey) w.* from wall w order by w.procedure_id, w.wkey) z
    group by 1
  ),
  la as (
    select l.procedure_id,
           bool_or(l.awarded) as any_awarded,
           bool_and(l.result_code in ('unsuccessful', 'clos-nw')) as all_unsuccessful,
           bool_and(l.result_code = 'discontinued') as all_discontinued,
           count(*) as n_lot_rows,
           max(l.n_lots_in_notice) as n_lots_notice,
           max(l.bids_received) as bids,
           bool_or(l.winners @> '[{"group": true}]'::jsonb) as grp,
           max(coalesce(l.award_date, l.contract_date)) as award_date,
           count(*) filter (where l.awarded) as n_awarded,
           count(*) filter (where l.awarded and l.value is not null) as n_valued,
           count(distinct l.currency) filter (where l.awarded and l.value is not null) as n_cur,
           max(l.currency) filter (where l.awarded and l.value is not null) as cur,
           sum(l.value) filter (where l.awarded and l.value is not null) as lot_value_sum
    from l group by 1
  ),
  one as (                                             -- single-lot procedure: its lot's quantity and strict unit price
    select l.procedure_id, l.quantity_used, l.quantity_used_unit, l.unit_price, l.currency
    from l join la on la.procedure_id = l.procedure_id
    where la.n_lot_rows = 1 and coalesce(l.n_lots_in_notice, 1) <= 1
  ),
  fam as (                                             -- family from the lots when the procedure had none
    select l.procedure_id, min(l.family_id) as family_id, min(l.class_id) as class_id, count(distinct l.family_id) as nf
    from l where l.family_id is not null and l.class_id in ('ammunition', 'firearms', 'parts', 'misc_weapons')
    group by 1
  ),
  cal as (
    select l.procedure_id, l.family_id, min(l.calibre) as calibre, count(distinct l.calibre) as nc
    from l where l.calibre is not null group by 1, 2
  )
  select p_snapshot, pp.procedure_id, pp.link_method, pp.eforms, pp.tender_publication_number, pp.competition_publication_numbers,
         pp.result_publication_numbers, pp.modification_publication_numbers,
         case when pp.outcome in ('result_only', 'no_result_yet') then pp.outcome
              when st.status in ('awarded', 'unsuccessful', 'cancelled') then st.status
              else pp.outcome end,
         st.status,
         case when wagg.winners @> '[{"source": "notice_xml"}]'::jsonb then 'notice_xml:winner'
              when st.from_xml then 'notice_xml:' || st.status
              else pp.outcome_basis end,
         pp.overdue, pp.legal_basis, pp.procedure_type, pp.buyer_name, pp.buyer_identifier, pp.buyer_country,
         pp.buyer_legal_type, pp.main_activity, pp.title_en, pp.title_original, pp.description, pp.cpv_codes,
         coalesce(pp.class_id, case when fam.nf = 1 then fam.class_id end),
         coalesce(pp.family_id, case when fam.nf = 1 then fam.family_id end),
         coalesce(pp.calibre, case when cal.nc = 1 then cal.calibre end),
         case when pp.family_id is null and fam.nf = 1 then 'lots' else pp.classification_method end,
         pp.classification_confidence,
         coalesce(pp.classification_detail, '{}'::jsonb) || jsonb_build_object('lots', jsonb_build_object(
           'n', la.n_lot_rows, 'families', (select jsonb_object_agg(z.f, z.n) from (select coalesce(l.family_id, 'undecided') f, count(*) n
                                             from l where l.procedure_id = pp.procedure_id group by 1) z),
           'family_from_lots', pp.family_id is null and fam.nf = 1)),
         pp.published, pp.deadline,
         coalesce(pp.decided, la.award_date),
         case when pp.decided is null and la.award_date is not null then 'notice_xml_award_date' else pp.decided_basis end,
         coalesce(pp.days_to_award,
                  case when pp.decided is null and la.award_date is not null and pp.published is not null and la.award_date >= pp.published
                       then la.award_date - pp.published end),
         pp.estimated_value, pp.estimated_currency,
         coalesce(pp.awarded_value, case when la.n_awarded > 0 and la.n_valued = la.n_awarded and la.n_cur = 1 then la.lot_value_sum end),
         case when pp.awarded_value is null and la.n_awarded > 0 and la.n_valued = la.n_awarded and la.n_cur = 1 then la.cur else pp.currency end,
         greatest(pp.n_lots, la.n_lots_notice),
         one.quantity_used, one.quantity_used_unit, one.unit_price,
         coalesce(la.bids, pp.bids_received),
         coalesce(wagg.winners, pp.winners),
         coalesce(pp.consortium, false) or coalesce(la.grp, false),
         pp.non_award_reasons
  from pp
  join la on la.procedure_id = pp.procedure_id
  left join wagg on wagg.procedure_id = pp.procedure_id
  left join one on one.procedure_id = pp.procedure_id
  left join fam on fam.procedure_id = pp.procedure_id
  left join cal on cal.procedure_id = pp.procedure_id and cal.family_id = coalesce(pp.family_id, case when fam.nf = 1 then fam.family_id end)
  cross join lateral (
    select case when pp.eforms and pp.result_status is distinct from 'unknown' then pp.result_status
                else coalesce(case when la.any_awarded then 'awarded' when la.all_discontinued then 'cancelled'
                                   when la.all_unsuccessful then 'unsuccessful' end, pp.result_status) end as status,
           not (pp.eforms and pp.result_status is distinct from 'unknown')
             and (la.any_awarded or la.all_discontinued or la.all_unsuccessful) as from_xml
  ) st;
  get diagnostics v_n = row_count;

  return jsonb_build_object(
    'overrides', v_n,
    'by_outcome_basis', (select jsonb_object_agg(z.b, z.n) from (select split_part(p.outcome_basis, ':', 1) || ':' || coalesce(split_part(p.outcome_basis, ':', 2), '') b,
                          count(*) n from public.kb_baseline_procedures p where p.snapshot_id = p_snapshot group by 1) z),
    'family_from_lots', (select count(*) from public.kb_baseline_procedures p where p.snapshot_id = p_snapshot and p.classification_method = 'lots'),
    'with_unit_price', (select count(*) from public.kb_baseline_procedures p where p.snapshot_id = p_snapshot and p.unit_price is not null));
end
$$;
revoke all on function public.kb_baseline_v2_procedures(text) from public, anon, authenticated;
grant execute on function public.kb_baseline_v2_procedures(text) to service_role;

-- ---------------------------------------------------------------------------------------------- unit-price points
-- One strict unit price per lot (snapshots with lot data) or per procedure without lot data (WS8 fix-forward rule:
-- single lot, one result notice, stated quantity > 1 in pieces, value in the procedure's currency).
create or replace view public.kb_baseline_unit_price_points with (security_invoker = on) as
select r.resolved_snapshot_id as snapshot_id, 'lot'::text as source, r.procedure_id, l.publication_number, l.lot_key,
       l.class_id, l.family_id, l.calibre, r.buyer_country as country, l.currency, l.unit_price,
       l.quantity_used as quantity, l.quantity_used_unit as quantity_unit, r.published,
       'ted:' || coalesce(r.tender_publication_number, r.result_publication_numbers[1]) as ref,
       l.unit_price_basis as basis
from public.kb_baseline_lots l
join public.kb_baseline_procedures_resolved r on r.resolved_snapshot_id = l.snapshot_id and r.procedure_id = l.procedure_id
where l.unit_price is not null and l.class_id is not null
union all
select r.resolved_snapshot_id, 'procedure', r.procedure_id, null, null,
       r.class_id, r.family_id, r.calibre, r.buyer_country, r.currency, r.unit_price,
       r.quantity, r.quantity_unit, r.published,
       'ted:' || coalesce(r.tender_publication_number, r.result_publication_numbers[1]),
       'awarded notice total / stated single-lot quantity (pieces, quantity > 1, one result notice)'
from public.kb_baseline_procedures_resolved r
where r.unit_price is not null and r.quantity > 1 and r.quantity_unit = 'H87' and r.currency is not null and r.class_id is not null
  and not exists (select 1 from public.kb_baseline_lots l where l.snapshot_id = r.resolved_snapshot_id and l.procedure_id = r.procedure_id);
grant select on public.kb_baseline_unit_price_points to anon, authenticated, service_role;

-- ---------------------------------------------------------------------------------------------- stats and patterns
-- As 20261030144734_kb_baseline_stats.sql, with three changes: the population is the resolved procedure set
-- (overlay + parent); unit-price columns come from kb_baseline_unit_price_points (strict, lot level where parsed;
-- family_country and family_calibre_country only with >= 3 procedures; same currency as awarded_value_p50, else
-- null); P001 and P002 count only the four focus classes.
create or replace function public.kb_baseline_build_stats(p_snapshot text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_status text;
  v_stats integer;
  v_patterns integer;
begin
  select s.status into v_status from public.kb_snapshots s where s.id = p_snapshot;
  if v_status is distinct from 'building' then
    raise exception 'kb_baseline_build_stats: snapshot % is % (must be building)', p_snapshot, coalesce(v_status, 'missing');
  end if;

  delete from public.kb_baseline_stats t where t.snapshot_id = p_snapshot;
  delete from public.kb_patterns t where t.snapshot_id = p_snapshot;

  create temp table if not exists kb_bs_p (like public.kb_baseline_procedures) on commit drop;
  truncate pg_temp.kb_bs_p;
  insert into pg_temp.kb_bs_p
  select * from public.kb_baseline_procedures_of(p_snapshot) pr where pr.class_id is not null;

  -- ---------------------------------------------------------------- stats
  insert into public.kb_baseline_stats (
    snapshot_id, scope_level, class_id, family_id, calibre, country, buyer_legal_type, n_procedures, n_awarded,
    per_year, quantity_p50, quantity_unit, n_unit_prices, unit_price_p25, unit_price_p50, unit_price_p75, currency,
    awarded_value_p50, value_published_share, bids_p50, top_winner_share, top_winners, procedure_types,
    days_to_award_p50, repeat_interval_days_p50, example_refs)
  with p as (
    select x.*,
           'ted:' || coalesce(x.tender_publication_number, x.result_publication_numbers[1], x.modification_publication_numbers[1]) as ref,
           x.result_status = 'awarded' as is_awarded,
           lower(regexp_replace(coalesce(x.buyer_name, ''), '[^[:alnum:]]+', ' ', 'g')) || '|' || coalesce(x.buyer_country, '') as buyer_key
    from pg_temp.kb_bs_p x
  ),
  scoped as materialized (
    select 'class' as lvl, p.class_id as s_class, null::text as s_family, null::text as s_calibre, null::text as s_country, p.*
    from p
    union all
    select 'family', p.class_id, p.family_id, null, null, p.* from p where p.family_id is not null
    union all
    select 'family_country', p.class_id, p.family_id, null, p.buyer_country, p.*
    from p where p.family_id is not null and p.buyer_country is not null
    union all
    select 'family_calibre_country', p.class_id, p.family_id, p.calibre, p.buyer_country, p.*
    from p where p.family_id is not null and p.calibre is not null and p.buyer_country is not null
  ),
  k as (
    select s.*, concat_ws('|', s.lvl, s.s_class, coalesce(s.s_family, ''), coalesce(s.s_calibre, ''), coalesce(s.s_country, '')) as sk
    from scoped s
  ),
  cur as (                                             -- dominant awarded currency, else estimated currency
    select k.sk,
           coalesce(mode() within group (order by k.currency) filter (where k.awarded_value is not null),
                    mode() within group (order by k.estimated_currency) filter (where k.estimated_value is not null)) as currency
    from k group by k.sk
  ),
  qunit as (                                           -- dominant quantity unit
    select k.sk, mode() within group (order by k.quantity_unit) filter (where k.quantity is not null) as unit
    from k group by k.sk
  ),
  upt as materialized (                                -- strict unit prices (lots where parsed, else procedures)
    select u.* from public.kb_baseline_unit_price_points u where u.snapshot_id = p_snapshot and u.quantity_unit = 'H87'
  ),
  ups as (
    select concat_ws('|', 'class', u.class_id, '', '', '') as sk, 'class' as lvl, u.* from upt u
    union all
    select concat_ws('|', 'family', u.class_id, u.family_id, '', ''), 'family', u.* from upt u where u.family_id is not null
    union all
    select concat_ws('|', 'family_country', u.class_id, u.family_id, '', u.country), 'family_country', u.*
    from upt u where u.family_id is not null and u.country is not null
    union all
    select concat_ws('|', 'family_calibre_country', u.class_id, u.family_id, u.calibre, u.country), 'family_calibre_country', u.*
    from upt u where u.family_id is not null and u.calibre is not null and u.country is not null
  ),
  upa as (
    select u.sk, count(*) as n_up, count(distinct u.procedure_id) as n_up_proc, max(u.lvl) as lvl,
           percentile_cont(0.25) within group (order by u.unit_price) as up25,
           percentile_cont(0.5) within group (order by u.unit_price) as up50,
           percentile_cont(0.75) within group (order by u.unit_price) as up75
    from ups u join cur c on c.sk = u.sk and u.currency = c.currency
    group by u.sk
  ),
  base as (
    select k.sk, k.lvl, k.s_class, k.s_family, k.s_calibre, k.s_country,
           count(*) as n,
           count(*) filter (where k.is_awarded) as n_awarded,
           avg(case when k.estimated_value is not null or k.awarded_value is not null then 1.0 else 0.0 end) as value_share,
           percentile_cont(0.5) within group (order by k.bids_received) filter (where k.bids_received is not null) as bids_p50,
           percentile_cont(0.5) within group (order by k.days_to_award)
             filter (where k.days_to_award between 0 and 1500) as days_p50,
           percentile_cont(0.5) within group (order by k.quantity) filter (where k.quantity is not null and k.quantity_unit is not distinct from qu.unit) as quantity_p50,
           max(qu.unit) as quantity_unit,
           max(c.currency) as currency,
           percentile_cont(0.5) within group (order by k.awarded_value) filter (where k.awarded_value is not null and k.currency = c.currency) as awarded_p50,
           (array_agg(k.ref order by k.is_awarded desc, k.published desc nulls last, k.ref))[1:5] as example_refs
    from k
    left join cur c on c.sk = k.sk
    left join qunit qu on qu.sk = k.sk
    group by k.sk, k.lvl, k.s_class, k.s_family, k.s_calibre, k.s_country
  ),
  years as (
    select y.sk, jsonb_object_agg(y.yr, y.n order by y.yr) as per_year
    from (select k.sk, coalesce(extract(year from k.published)::int::text, 'unknown') as yr, count(*) as n
          from k group by 1, 2) y
    group by y.sk
  ),
  ptypes as (
    select t.sk, jsonb_object_agg(t.pt, t.n order by t.n desc, t.pt) as procedure_types
    from (select k.sk, coalesce(k.procedure_type, 'unknown') as pt, count(*) as n from k group by 1, 2) t
    group by t.sk
  ),
  wins as (                                            -- one row per (scope, winner, procedure)
    select distinct k.sk, k.procedure_id, lower(w ->> 'name') as wkey, w ->> 'name' as name, w ->> 'country' as country
    from k, jsonb_array_elements(k.winners) w
    where k.is_awarded
  ),
  wcount as (
    select w.sk, w.wkey, max(w.name) as name, max(w.country) as country, count(distinct w.procedure_id) as wins,
           row_number() over (partition by w.sk order by count(distinct w.procedure_id) desc, w.wkey) as rk
    from wins w group by w.sk, w.wkey
  ),
  wtot as (
    select w.sk, count(distinct w.procedure_id) as n_with_winner from wins w group by w.sk
  ),
  topw as (
    select c.sk,
           jsonb_agg(jsonb_build_object('name', c.name, 'country', c.country, 'wins', c.wins) order by c.rk) as top_winners,
           max(c.wins) filter (where c.rk = 1) as top_wins
    from wcount c where c.rk <= 5 group by c.sk
  ),
  gaps as (
    select k.sk, k.published - lag(k.published) over (partition by k.sk, k.buyer_key order by k.published, k.procedure_id) as gap
    from k where k.published is not null
  ),
  rep as (
    select g.sk, percentile_cont(0.5) within group (order by g.gap) as gap_p50
    from gaps g where g.gap > 0 group by g.sk
  )
  select p_snapshot, b.lvl, b.s_class, b.s_family, b.s_calibre, b.s_country, null,
         b.n, b.n_awarded, y.per_year,
         b.quantity_p50, case when b.quantity_p50 is not null then b.quantity_unit end,
         coalesce(case when u.lvl in ('class', 'family') or u.n_up_proc >= 3 then u.n_up end, 0),
         case when u.lvl in ('class', 'family') or u.n_up_proc >= 3 then round(u.up25::numeric, 4) end,
         case when u.lvl in ('class', 'family') or u.n_up_proc >= 3 then round(u.up50::numeric, 4) end,
         case when u.lvl in ('class', 'family') or u.n_up_proc >= 3 then round(u.up75::numeric, 4) end,
         b.currency,
         round(b.awarded_p50::numeric, 2),
         round(b.value_share, 3),
         b.bids_p50,
         case when wt.n_with_winner > 0 then round(tp.top_wins::numeric / wt.n_with_winner, 3) end,
         tp.top_winners,
         pt.procedure_types,
         round(b.days_p50)::int,
         round(r.gap_p50)::int,
         coalesce(b.example_refs, '{}')
  from base b
  left join years y on y.sk = b.sk
  left join ptypes pt on pt.sk = b.sk
  left join topw tp on tp.sk = b.sk
  left join wtot wt on wt.sk = b.sk
  left join rep r on r.sk = b.sk
  left join upa u on u.sk = b.sk
  where b.lvl in ('class', 'family') or b.n >= 3;
  get diagnostics v_stats = row_count;

  -- ---------------------------------------------------------------- patterns
  -- P001 demand: buyers re-buying the same family, with the interval (four focus classes only)
  insert into public.kb_patterns (snapshot_id, id, kind, scope, statement, support, examples, metrics)
  with p as (
    select x.*, 'ted:' || coalesce(x.tender_publication_number, x.result_publication_numbers[1], x.modification_publication_numbers[1]) as ref,
           lower(regexp_replace(coalesce(x.buyer_name, ''), '[^[:alnum:]]+', ' ', 'g')) || '|' || coalesce(x.buyer_country, '') as buyer_key
    from pg_temp.kb_bs_p x
    where x.family_id is not null and x.class_id in ('ammunition', 'firearms', 'parts', 'misc_weapons') and x.published is not null
  ),
  g as (
    select p.*, p.published - lag(p.published) over (partition by p.buyer_key, p.family_id order by p.published, p.procedure_id) as gap,
           count(*) over (partition by p.buyer_key, p.family_id) as n_pair
    from p
  ),
  pairs as (
    select g.family_id, g.buyer_key, max(g.buyer_name) as buyer_name, max(g.buyer_country) as buyer_country,
           count(*) as n, (array_agg(g.ref order by g.published desc))[1:5] as refs
    from g group by g.family_id, g.buyer_key
  ),
  fam as (
    select pr.family_id,
           count(*) as buyers,
           count(*) filter (where pr.n >= 2) as repeat_buyers,
           (select percentile_cont(0.5) within group (order by g.gap) from g where g.family_id = pr.family_id and g.gap > 0) as gap_p50,
           (array_agg(jsonb_build_object('buyer', pr.buyer_name, 'country', pr.buyer_country, 'procedures', pr.n)
                      order by pr.n desc, pr.buyer_name) filter (where pr.n >= 2))[1:3] as top_buyers,
           (array_agg(to_jsonb(pr.refs) order by pr.n desc, pr.buyer_name) filter (where pr.n >= 2))[1] as refs
    from pairs pr group by pr.family_id
  ),
  glob as (
    select count(*) as pairs, count(*) filter (where pr.n >= 2) as repeat_pairs,
           (select percentile_cont(0.5) within group (order by g.gap) from g where g.gap > 0) as gap_p50,
           (select min(published) from p) as since, (select max(published) from p) as until
    from pairs pr
  )
  select p_snapshot, 'P001', 'demand', jsonb_build_object('classes', jsonb_build_array('ammunition', 'firearms', 'parts', 'misc_weapons')),
         format('In the four focus classes (ammunition, firearms, parts, misc weapons), %s of %s buyer-family pairs bought the same product family at least twice between %s and %s; the median interval between two purchases is %s days. Most re-bought: %s.',
                gl.repeat_pairs, gl.pairs, gl.since, gl.until, round(gl.gap_p50),
                (select string_agg(format('%s (%s repeat buyers)', f.family_id, f.repeat_buyers), ', ' order by f.repeat_buyers desc)
                 from (select * from fam order by repeat_buyers desc limit 3) f)),
         gl.repeat_pairs::int,
         coalesce((select array(select jsonb_array_elements_text(f.refs)) from fam f order by f.repeat_buyers desc limit 1), '{}'),
         jsonb_build_object('buyer_family_pairs', gl.pairs, 'repeat_pairs', gl.repeat_pairs,
                            'repeat_share', round(gl.repeat_pairs::numeric / nullif(gl.pairs, 0), 3),
                            'interval_days_p50', round(gl.gap_p50),
                            'by_family', (select jsonb_agg(jsonb_build_object('family_id', f.family_id, 'buyers', f.buyers,
                                                  'repeat_buyers', f.repeat_buyers, 'interval_days_p50', round(f.gap_p50))
                                                  order by f.repeat_buyers desc) from fam f where f.repeat_buyers > 0))
  from glob gl
  union all
  select p_snapshot, 'P001:' || f.family_id, 'demand', jsonb_build_object('family_id', f.family_id),
         format('%s of %s buyers of %s bought it again; median interval %s days. Top repeat buyers: %s.',
                f.repeat_buyers, f.buyers, f.family_id, coalesce(round(f.gap_p50)::text, 'unknown'),
                (select string_agg(format('%s (%s, %s procedures)', b ->> 'buyer', b ->> 'country', b ->> 'procedures'), '; ')
                 from unnest(f.top_buyers) b)),
         f.repeat_buyers::int, coalesce(array(select jsonb_array_elements_text(f.refs)), '{}'),
         jsonb_build_object('buyers', f.buyers, 'repeat_buyers', f.repeat_buyers, 'interval_days_p50', round(f.gap_p50),
                            'top_buyers', to_jsonb(f.top_buyers))
  from fam f where f.repeat_buyers >= 3;

  -- P002 supplier: winner concentration per family and buyer country (awards with named winners; four focus classes)
  insert into public.kb_patterns (snapshot_id, id, kind, scope, statement, support, examples, metrics)
  with w as (
    select distinct x.procedure_id, x.family_id, x.buyer_country, lower(e ->> 'name') as wkey, e ->> 'name' as name,
           e ->> 'country' as wcountry,
           'ted:' || coalesce(x.tender_publication_number, x.result_publication_numbers[1]) as ref, x.published
    from pg_temp.kb_bs_p x, jsonb_array_elements(x.winners) e
    where x.result_status = 'awarded' and x.family_id is not null and x.class_id in ('ammunition', 'firearms', 'parts', 'misc_weapons')
      and x.buyer_country is not null
  ),
  mk as (
    select w.family_id, w.buyer_country, count(distinct w.procedure_id) as n, count(distinct w.wkey) as winners
    from w group by 1, 2
  ),
  wc as (
    select w.family_id, w.buyer_country, w.wkey, max(w.name) as name, max(w.wcountry) as wcountry,
           count(distinct w.procedure_id) as wins, (array_agg(w.ref order by w.published desc))[1:5] as refs,
           row_number() over (partition by w.family_id, w.buyer_country order by count(distinct w.procedure_id) desc, w.wkey) as rk
    from w group by 1, 2, 3
  ),
  m as (
    select mk.*, wc.name as top_name, wc.wcountry as top_country, wc.wins as top_wins,
           round(wc.wins::numeric / mk.n, 3) as top_share, wc.refs
    from mk join wc on wc.family_id = mk.family_id and wc.buyer_country = mk.buyer_country and wc.rk = 1
    where mk.n >= 5
  )
  select p_snapshot, 'P002', 'supplier', jsonb_build_object('classes', jsonb_build_array('ammunition', 'firearms', 'parts', 'misc_weapons')),
         format('In %s family-country markets of the four focus classes with at least 5 awards to named winners (eForms results from Nov 2023, plus pre-eForms results whose winners were read from the notice XML), the largest winner takes a median %s%% of the awards. Most concentrated: %s.',
                count(*), round(100 * percentile_cont(0.5) within group (order by m.top_share)),
                string_agg(format('%s in %s: %s %s%% of %s', m.family_id, m.buyer_country, m.top_name, round(100 * m.top_share), m.n), '; '
                           order by m.top_share desc, m.n desc) filter (where m.top_share >= 0.5)),
         count(*)::int,
         (array_agg(m.refs[1] order by m.top_share desc, m.n desc))[1:5],
         jsonb_build_object('markets', count(*), 'top_share_p50', round((percentile_cont(0.5) within group (order by m.top_share))::numeric, 3),
                            'markets_top_share_ge_50pct', count(*) filter (where m.top_share >= 0.5),
                            'by_market', jsonb_agg(jsonb_build_object('family_id', m.family_id, 'country', m.buyer_country,
                                         'awards', m.n, 'distinct_winners', m.winners, 'top_winner', m.top_name,
                                         'top_winner_country', m.top_country, 'top_share', m.top_share) order by m.top_share desc, m.n desc))
  from m
  having count(*) > 0
  union all
  select p_snapshot, 'P002:' || m.family_id || ':' || m.buyer_country, 'supplier',
         jsonb_build_object('family_id', m.family_id, 'country', m.buyer_country),
         format('%s in %s: %s awards to %s distinct winners; %s (%s) won %s (%s%%).',
                m.family_id, m.buyer_country, m.n, m.winners, m.top_name, coalesce(m.top_country, 'country unknown'),
                m.top_wins, round(100 * m.top_share)),
         m.n::int, coalesce(m.refs, '{}'),
         jsonb_build_object('awards', m.n, 'distinct_winners', m.winners, 'top_winner', m.top_name,
                            'top_winner_country', m.top_country, 'top_wins', m.top_wins, 'top_share', m.top_share)
  from m;

  -- P003 qualification: recurring requirements named in the tender text (multilingual stems)
  insert into public.kb_patterns (snapshot_id, id, kind, scope, statement, support, examples, metrics)
  with req(code, label, pattern) as (
    values
      ('aqap', 'NATO AQAP quality assurance (AQAP 2110/2310 ...)', 'aqap'),
      ('iso9001', 'ISO 9001 quality management', 'iso[[:space:]-]*9001'),
      ('stanag', 'NATO STANAG conformity', 'stanag'),
      ('security_clearance', 'Facility or personnel security clearance',
       '(security clearance|facility security|habilitation|świadectw[[:alpha:]]* bezpiecze|poświadczen[[:alpha:]]* bezpiecze|sicherheitsbescheid|sicherheitsüberprüf|nulla osta di sicurezza|bezpečnostní|turvallisuusselvity|säkerhetsskydd)'),
      ('licence', 'Arms trade licence or concession', '(koncesj|arms licen|weapons licen|waffenhandelserlaubnis|zbrojní licenc|licencia de armas|autorisation de fabrication|munitions licen|export licen)'),
      ('end_user', 'End-user certificate', '(end[[:space:]-]?user|end[[:space:]-]?use certificate|utilisateur final|endverbleib|eindgebruik|użytkownika końcowego)'),
      ('nsn', 'NATO stock number / codification', '(nato stock number|[[:<:]]nsn[[:>:]]|kodyfikac|codification)'),
      ('cip_proof', 'C.I.P. proof marking', '(c\.i\.p\.|[[:<:]]cip[[:>:]]|proof mark|beschuss|cechowan)')
  ),
  t as (
    select x.procedure_id, x.family_id, 'ted:' || coalesce(x.tender_publication_number, x.result_publication_numbers[1]) as ref,
           x.published, lower(concat_ws(' ', x.title_original, x.description)) as txt
    from pg_temp.kb_bs_p x
    where x.description is not null and (x.family_id is null or x.family_id <> 'out_of_scope.noise')
  ),
  hits as (
    select r.code, r.label, t.procedure_id, t.family_id, t.ref, t.published
    from t join req r on t.txt ~ r.pattern
  ),
  per as (
    select h.code, max(h.label) as label, count(distinct h.procedure_id) as n,
           (array_agg(h.ref order by h.published desc))[1:5] as refs,
           (select jsonb_object_agg(z.family_id, z.c) from (select h2.family_id, count(*) c from hits h2
             where h2.code = h.code and h2.family_id is not null group by h2.family_id order by count(*) desc limit 5) z) as top_families
    from hits h group by h.code
  ),
  tot as (select count(*) as with_text, (select count(distinct procedure_id) from hits) as with_req from t)
  select p_snapshot, 'P003', 'qualification', '{}'::jsonb,
         format('%s of %s procedures with tender text name at least one qualification requirement. Most frequent: %s.',
                tot.with_req, tot.with_text,
                (select string_agg(format('%s (%s)', per.label, per.n), ', ' order by per.n desc) from per)),
         tot.with_req::int,
         coalesce((select per.refs from per order by per.n desc limit 1), '{}'),
         jsonb_build_object('procedures_with_text', tot.with_text, 'procedures_with_requirement', tot.with_req,
                            'by_requirement', (select jsonb_agg(jsonb_build_object('code', per.code, 'label', per.label, 'procedures', per.n,
                                               'share', round(per.n::numeric / nullif(tot.with_text, 0), 3), 'top_families', per.top_families)
                                               order by per.n desc) from per),
                            'method', 'regex stems over original title + description (eForms notices carry description; legacy notices only a title)')
  from tot
  where tot.with_req > 0
  union all
  select p_snapshot, 'P003:' || per.code, 'qualification', jsonb_build_object('requirement', per.code),
         format('%s: named in %s of %s procedures with tender text (%s%%); most often for %s.',
                per.label, per.n, tot.with_text, round(100.0 * per.n / nullif(tot.with_text, 0), 1),
                coalesce((select string_agg(k, ', ') from jsonb_object_keys(per.top_families) k), 'unclassified procedures')),
         per.n::int, coalesce(per.refs, '{}'),
         jsonb_build_object('procedures', per.n, 'share', round(per.n::numeric / nullif(tot.with_text, 0), 3), 'top_families', per.top_families)
  from per, tot
  where per.n >= 5;

  -- P004 collaboration: consortium wins (one tendering party, several winners)
  insert into public.kb_patterns (snapshot_id, id, kind, scope, statement, support, examples, metrics)
  with a as (
    select x.*, 'ted:' || coalesce(x.tender_publication_number, x.result_publication_numbers[1]) as ref,
           (select count(distinct e ->> 'country') from jsonb_array_elements(x.winners) e) as winner_countries
    from pg_temp.kb_bs_p x
    where x.result_status = 'awarded' and jsonb_array_length(x.winners) > 0
  ),
  fam as (
    select a.family_id, count(*) as n, count(*) filter (where a.consortium) as consortia
    from a where a.family_id is not null group by a.family_id
  )
  select p_snapshot, 'P004', 'collaboration', '{}'::jsonb,
         format('%s of %s awarded procedures with named winners (%s%%) were won by a consortium (one tendering party, several winners); %s of these consortia crossed borders. %s%% of awards name more than one winner (consortia, multi-lot or multi-supplier frameworks).',
                count(*) filter (where a.consortium), count(*),
                round(100.0 * count(*) filter (where a.consortium) / nullif(count(*), 0), 1),
                count(*) filter (where a.consortium and a.winner_countries > 1),
                round(100.0 * count(*) filter (where jsonb_array_length(a.winners) > 1) / nullif(count(*), 0), 1)),
         (count(*) filter (where a.consortium))::int,
         coalesce((array_agg(a.ref order by a.published desc) filter (where a.consortium))[1:5], '{}'),
         jsonb_build_object('awarded_with_winner', count(*), 'consortium', count(*) filter (where a.consortium),
                            'consortium_share', round(count(*) filter (where a.consortium)::numeric / nullif(count(*), 0), 3),
                            'cross_border_consortium', count(*) filter (where a.consortium and a.winner_countries > 1),
                            'multi_winner_share', round(count(*) filter (where jsonb_array_length(a.winners) > 1)::numeric / nullif(count(*), 0), 3),
                            'by_family', (select jsonb_agg(jsonb_build_object('family_id', f.family_id, 'awards', f.n, 'consortia', f.consortia)
                                          order by f.consortia desc, f.n desc) from fam f where f.consortia > 0),
                            'method', 'consortium = a result with more winners than tendering parties (eForms) or a joint tender / AWARDED_TO_GROUP in the notice XML')
  from a
  having count(*) > 0;

  -- P005 pricing: awarded value bands per family (dominant currency; notice totals, not lot prices)
  insert into public.kb_patterns (snapshot_id, id, kind, scope, statement, support, examples, metrics)
  with v as (
    select x.family_id, x.currency, x.awarded_value, x.published,
           'ted:' || coalesce(x.tender_publication_number, x.result_publication_numbers[1]) as ref
    from pg_temp.kb_bs_p x
    where x.result_status = 'awarded' and x.awarded_value > 0 and x.currency is not null
      and x.family_id is not null and x.family_id <> 'out_of_scope.noise'
  ),
  b as (
    select v.family_id, v.currency, count(*) as n,
           percentile_cont(0.1) within group (order by v.awarded_value) as p10,
           percentile_cont(0.25) within group (order by v.awarded_value) as p25,
           percentile_cont(0.5) within group (order by v.awarded_value) as p50,
           percentile_cont(0.75) within group (order by v.awarded_value) as p75,
           percentile_cont(0.9) within group (order by v.awarded_value) as p90,
           (array_agg(v.ref order by v.published desc))[1:5] as refs
    from v group by v.family_id, v.currency
    having count(*) >= 5
  )
  select p_snapshot, 'P005', 'pricing', '{}'::jsonb,
         format('Awarded values (notice totals) form stable bands per family and currency in %s family-currency groups with at least 5 awards. Examples: %s.',
                count(*),
                string_agg(format('%s %s median %s (p25 %s, p75 %s, n=%s)', b.family_id, b.currency,
                                  to_char(round(b.p50), 'FM999G999G999G990'), to_char(round(b.p25), 'FM999G999G999G990'),
                                  to_char(round(b.p75), 'FM999G999G999G990'), b.n), '; ' order by b.n desc)
                  filter (where b.n >= 15)),
         sum(b.n)::int,
         (array_agg(b.refs[1] order by b.n desc))[1:5],
         jsonb_build_object('groups', count(*),
                            'by_group', jsonb_agg(jsonb_build_object('family_id', b.family_id, 'currency', b.currency, 'n', b.n,
                                          'p10', round(b.p10), 'p25', round(b.p25), 'p50', round(b.p50), 'p75', round(b.p75), 'p90', round(b.p90))
                                          order by b.n desc),
                            'basis', 'awarded_value = sum of result notice totals in the dominant currency (pre-eForms notices without a total: sum of lot values from the notice XML); multi-lot notices are not split')
  from b
  having count(*) > 0
  union all
  select p_snapshot, 'P005:' || b.family_id || ':' || b.currency, 'pricing',
         jsonb_build_object('family_id', b.family_id, 'currency', b.currency),
         format('%s awards in %s (n=%s, notice totals): p10 %s, p25 %s, median %s, p75 %s, p90 %s.',
                b.family_id, b.currency, b.n,
                to_char(round(b.p10), 'FM999G999G999G990'), to_char(round(b.p25), 'FM999G999G999G990'),
                to_char(round(b.p50), 'FM999G999G999G990'), to_char(round(b.p75), 'FM999G999G999G990'),
                to_char(round(b.p90), 'FM999G999G999G990')),
         b.n::int, coalesce(b.refs, '{}'),
         jsonb_build_object('n', b.n, 'p10', round(b.p10), 'p25', round(b.p25), 'p50', round(b.p50), 'p75', round(b.p75), 'p90', round(b.p90),
                            'basis', 'notice total')
  from b;

  select count(*) into v_patterns from public.kb_patterns t where t.snapshot_id = p_snapshot;

  return jsonb_build_object(
    'stats', v_stats,
    'stats_by_level', (select jsonb_object_agg(z.scope_level, z.c) from (select t.scope_level, count(*) c from public.kb_baseline_stats t
                        where t.snapshot_id = p_snapshot group by t.scope_level) z),
    'stats_with_unit_price', (select count(*) from public.kb_baseline_stats t where t.snapshot_id = p_snapshot and t.unit_price_p50 is not null),
    'patterns', v_patterns,
    'patterns_by_kind', (select jsonb_object_agg(z.kind, z.c) from (select t.kind, count(*) c from public.kb_patterns t
                          where t.snapshot_id = p_snapshot group by t.kind) z));
end
$$;
revoke all on function public.kb_baseline_build_stats(text) from public, anon, authenticated;
grant execute on function public.kb_baseline_build_stats(text) to service_role;

-- ---------------------------------------------------------------------------------------------- card visibility
-- Is a card of the active snapshot's parent still visible? Hidden when the active snapshot has a card with the same
-- kind and source_key, any award card for the same result notice (winners re-read from the XML), or (patterns) any
-- pattern card of its own.
create or replace function public.kb_baseline_inherited(p_active text, p_kind text, p_source_key text, p_publication_number text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select not exists (select 1 from public.kb_chunks o
                     where o.kind = p_kind and o.source_key = p_source_key and o.layer = 'baseline' and o.snapshot_id = p_active)
     and not (p_kind = 'pattern' and exists (select 1 from public.kb_chunks o
                                             where o.kind = 'pattern' and o.layer = 'baseline' and o.snapshot_id = p_active))
     and not (p_kind = 'award' and p_publication_number is not null
              and exists (select 1 from public.kb_chunks o
                          where o.publication_number = p_publication_number and o.kind = 'award'
                            and o.layer = 'baseline' and o.snapshot_id = p_active))
$$;
revoke all on function public.kb_baseline_inherited(text, text, text, text) from public, anon, authenticated;
grant execute on function public.kb_baseline_inherited(text, text, text, text) to service_role;

-- ---------------------------------------------------------------------------------------------- cards
-- As 20261030150246_kb_baseline_chunks.sql for a snapshot without parent. For an overlay snapshot every card is built
-- from the resolved procedures (tender cards of procedures with parsed lots gain a "Lots" section; award cards of
-- parsed result notices come from the XML winners, per winner with the lots and values won), then written only when
--   - it is new or its text changed (embedded by kb-embed), or
--   - it is unchanged but its ref is also a live TED row (ted_notices / ted_awards): the parent card is copied with
--     its embedding, so kb_build_tender / kb_build_award keep finding the ref in the active baseline;
-- all other parent cards stay visible through kb_baseline_inherited. Pattern cards are always the snapshot's own.
create or replace function public.kb_baseline_build_chunks(p_snapshot text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_status text;
  v_cutoff date;
  v_parent text;
  v_source text;
  v_parent_source text;
  v_built jsonb;
  v_written jsonb;
begin
  select s.status, s.cutoff_date, s.parent_id into v_status, v_cutoff, v_parent from public.kb_snapshots s where s.id = p_snapshot;
  if v_status is distinct from 'building' then
    raise exception 'kb_baseline_build_chunks: snapshot % is % (must be building)', p_snapshot, coalesce(v_status, 'missing');
  end if;
  v_source := 'Source: frozen TED baseline ' || p_snapshot || ' (TED history up to ' || v_cutoff || ')';
  if v_parent is not null then
    select 'Source: frozen TED baseline ' || s.id || ' (TED history up to ' || s.cutoff_date || ')' into v_parent_source
    from public.kb_snapshots s where s.id = v_parent;
  end if;

  delete from public.kb_chunks k where k.layer = 'baseline' and k.snapshot_id = p_snapshot;

  drop table if exists pg_temp.kb_bc;
  create temp table kb_bc (kind text, source_key text, title text, content text, facts jsonb, publication_number text,
    supplier_id text, buyer_id text, class_id text, family_id text, calibre text, cpv_codes text[], country text,
    occurred_at date) on commit drop;
  drop table if exists pg_temp.kb_bc_p;
  create temp table kb_bc_p on commit drop as
    select * from public.kb_baseline_procedures_of(p_snapshot) p where p.class_id is not null;
  drop table if exists pg_temp.kb_bc_l;
  create temp table kb_bc_l on commit drop as
    select * from public.kb_baseline_lots l where l.snapshot_id = p_snapshot and l.procedure_id is not null;

  -- tender cards: one per procedure, with the outcome line (and the lots, where parsed)
  insert into pg_temp.kb_bc
  select 'tender', 'ted:' || x.pub,
         left(coalesce(nullif(btrim(p.title_en), ''), nullif(btrim(p.title_original), ''), 'Procedure ' || x.pub), 300),
         left(concat_ws(E'\n',
           'TENDER ' || x.pub || ' · ' || coalesce(p.buyer_country, 'country not stated') || ' · '
             || coalesce(nullif(btrim(p.buyer_name), ''), 'buyer not stated') || ' · '
             || coalesce(p.procedure_type, 'procedure not stated') || ' · legal basis ' || coalesce(p.legal_basis, 'not stated'),
           'What: ' || public.kb_what(p.family_id, p.class_id, p.calibre),
           'Title: ' || coalesce(nullif(btrim(p.title_en), ''), 'not published'),
           case when nullif(btrim(p.title_original), '') is not null and p.title_original is distinct from p.title_en
                then 'Original: ' || btrim(p.title_original) end,
           case when btrim(p.description) is distinct from btrim(p.title_en)
                 and btrim(p.description) is distinct from btrim(p.title_original)
                then nullif(left(btrim(p.description), case when lt.lots_text is null then 1000 else 600 end), '') end,
           case when p.tender_publication_number is not null then
             'Deadline: ' || coalesce(public.kb_fmt_ts(p.deadline), 'not published') end,
           case when p.tender_publication_number is not null then
             'Estimated value: ' || coalesce(public.kb_fmt_money(p.estimated_value, p.estimated_currency), 'not published') end,
           lt.lots_text,
           'Outcome: ' || x.outcome_text,
           v_source
         ), 2000),
         jsonb_strip_nulls(jsonb_build_object(
           'snapshot_id', p_snapshot, 'layer', 'baseline', 'procedure_id', p.procedure_id,
           'publication_number', x.pub, 'tender_publication_number', p.tender_publication_number,
           'result_publication_numbers', to_jsonb(p.result_publication_numbers),
           'ted_url', 'https://ted.europa.eu/en/notice/-/detail/' || x.pub,
           'published', p.published, 'decided', p.decided, 'decided_basis', p.decided_basis,
           'outcome', p.outcome, 'result_status', p.result_status, 'outcome_basis', p.outcome_basis, 'overdue', p.overdue,
           'link_method', p.link_method,
           'buyer', p.buyer_name, 'buyer_id', x.buyer_id, 'country', p.buyer_country, 'buyer_legal_type', p.buyer_legal_type,
           'procedure_type', p.procedure_type, 'legal_basis', p.legal_basis,
           'class_id', p.class_id, 'family_id', p.family_id, 'calibre', p.calibre,
           'classification_method', p.classification_method, 'confidence', round(p.classification_confidence::numeric, 2),
           'estimated_value', p.estimated_value, 'estimated_currency', p.estimated_currency,
           'awarded_value', p.awarded_value, 'currency', p.currency,
           'value_basis', case when p.awarded_value is not null then x.value_basis end,
           'days_to_award', p.days_to_award, 'bids_received', p.bids_received,
           'winners', case when jsonb_array_length(p.winners) > 0 then p.winners end, 'consortium', p.consortium,
           'n_lots', p.n_lots, 'quantity', p.quantity, 'quantity_unit', p.quantity_unit, 'unit_price', p.unit_price,
           'non_award_reasons', case when cardinality(p.non_award_reasons) > 0 then to_jsonb(p.non_award_reasons) end,
           'lots', lt.lots_facts
         )),
         x.pub, null, x.buyer_id, p.class_id, p.family_id, p.calibre, p.cpv_codes,
         public.kb_country(p.buyer_country), coalesce(p.published, p.decided)
  from pg_temp.kb_bc_p p
  left join public.kb_baseline_procedures pv on pv.snapshot_id = v_parent and pv.procedure_id = p.procedure_id and p.snapshot_id = p_snapshot
  left join lateral (
    select 'Lots (from the notice XML; ' || count(*) || ' lot result(s)' || case when count(*) > 8 then ', first 8 shown' else '' end || '):'
             || E'\n' || string_agg(z.line, E'\n' order by z.ord) filter (where z.ord <= 8) as lots_text,
           jsonb_agg(z.fact order by z.ord) filter (where z.ord <= 12) as lots_facts
    from (
      select row_number() over (order by l.publication_number, l.lot_id nulls last, l.lot_key) as ord,
             '- ' || coalesce(l.lot_id, l.lot_key) || coalesce(' ' || left(btrim(l.title), 90), '')
               || ' · ' || public.kb_what(l.family_id, l.class_id, l.calibre)
               || coalesce(' · ' || trim_scale(l.quantity_used) || ' '
                           || case l.quantity_used_unit when 'H87' then 'pieces' else coalesce(l.quantity_used_unit, '') end, '')
               || case when l.awarded then
                    ' · awarded to ' || coalesce((select string_agg(distinct (w ->> 'name') || coalesce(' (' || (w ->> 'country') || ')', ''), ', ')
                                                   from jsonb_array_elements(l.winners) w), 'a winner not named')
                    || coalesce(' for ' || public.kb_fmt_money(l.value, l.currency), '')
                    || coalesce(' (' || trim_scale(round(l.unit_price, 4)) || ' ' || l.currency || ' per piece)', '')
                    || case when l.framework then ' · framework agreement' else '' end
                  else ' · not awarded (' || coalesce(l.result_code, 'no result') || ')' end
               || coalesce(' · ' || l.bids_received || ' bid(s)', '') as line,
             jsonb_strip_nulls(jsonb_build_object(
               'notice', l.publication_number, 'lot', coalesce(l.lot_id, l.lot_key), 'title', left(l.title, 120),
               'family_id', l.family_id, 'calibre', l.calibre, 'quantity', l.quantity_used, 'unit', l.quantity_used_unit,
               'quantity_source', l.quantity_source, 'awarded', l.awarded, 'value', l.value, 'currency', l.currency,
               'value_source', l.value_source, 'unit_price', l.unit_price, 'framework', l.framework,
               'framework_max', l.framework_max, 'bids', l.bids_received,
               'winners', (select jsonb_agg(w ->> 'name') from jsonb_array_elements(l.winners) w))) as fact
      from pg_temp.kb_bc_l l
      where l.procedure_id = p.procedure_id and p.snapshot_id = p_snapshot
    ) z
    having count(*) > 0
  ) lt on true
  cross join lateral (
    select coalesce(p.tender_publication_number, p.result_publication_numbers[1], p.modification_publication_numbers[1]) as pub,
           public.product_bridge_match_buyer(p.buyer_identifier, p.buyer_name, p.buyer_country) as buyer_id,
           case when pv.procedure_id is not null and pv.awarded_value is null and p.awarded_value is not null
                then 'sum_of_lot_values_xml' else 'notice_total' end as value_basis,
           case
             when p.result_status = 'awarded' then
               'awarded to '
               || coalesce((select string_agg((w ->> 'name') || coalesce(' (' || (w ->> 'country') || ')', ''), ', ' order by o)
                            from jsonb_array_elements(p.winners) with ordinality a(w, o) where o <= 3)
                           || case when jsonb_array_length(p.winners) > 3
                                   then ' and ' || (jsonb_array_length(p.winners) - 3) || ' more' else '' end,
                           case when p.eforms then 'a winner not named in the notice'
                                else 'a winner not named in the TED Search API (legacy notice)' end)
               || ' for ' || coalesce(public.kb_fmt_money(p.awarded_value, p.currency)
                                      || case when pv.procedure_id is not null and pv.awarded_value is null and p.awarded_value is not null
                                              then ' (sum of lot values)' else ' (notice total)' end, 'a value not published')
               || coalesce(' after ' || p.days_to_award || ' days', '')
               || coalesce(', ' || trim_scale(p.bids_received) || ' bid(s)', ', bids not published')
               || coalesce(' · decided ' || p.decided, '')
               || case when p.outcome = 'result_only' then ' (tender notice not in the snapshot)' else '' end
             when p.result_status = 'unsuccessful' then
               'no award, unsuccessful (' || coalesce(public.kb_baseline_reason_text(p.non_award_reasons), 'no reason given') || ')'
               || coalesce(' · closed ' || p.decided, '')
             when p.result_status = 'cancelled' then
               'cancelled (' || coalesce(public.kb_baseline_reason_text(p.non_award_reasons), 'no reason given') || ')'
               || coalesce(' · closed ' || p.decided, '')
             when p.result_status = 'pending' then
               'result notice published, but no winner chosen yet (lots still open)'
             when p.result_status = 'unknown' then
               'result notice published; winner and value are not in the TED Search API (legacy notice)'
               || case when p.outcome = 'result_only' then ' (tender notice not in the snapshot)' else '' end
             when p.outcome_basis like 'no_linked_result%' then
               'no linked result notice (legacy notice: TED gives no procedure link, so a result may exist unlinked)'
             else
               'no result notice published by ' || v_cutoff
               || case when p.overdue then ' (more than 12 months after the deadline)' else '' end
           end as outcome_text
  ) x;

  -- award cards A: result notices without parsed lots (Search API winners; same template as baseline_v1)
  insert into pg_temp.kb_bc
  select 'award', a.key,
         left(a.winner_name || ' won ' || a.pub || ' · '
              || coalesce(nullif(btrim(p.title_en), ''), nullif(btrim(p.title_original), ''), 'award'), 300),
         left(concat_ws(E'\n',
           'AWARD ' || a.pub || ' · ' || coalesce(p.buyer_country, 'country not stated') || ' · buyer '
             || coalesce(nullif(btrim(p.buyer_name), ''), 'not stated') || ' → winner ' || a.winner_name
             || ' (' || coalesce(a.winner_country, 'country not stated') || ', ' || coalesce(a.winner_size, 'size not stated') || ')',
           'What: ' || public.kb_what(p.family_id, p.class_id, p.calibre),
           coalesce(nullif(btrim(p.title_en), ''), nullif(btrim(p.title_original), '')),
           nullif(left(btrim(p.description), 600), ''),
           'Notice value: ' || coalesce(public.kb_fmt_money(a.notice_value, a.currency), 'not published')
             || ' (notice total, ' || a.co_winners || ' winner(s))',
           'Decided: ' || coalesce(a.decision_date::text, 'not stated')
             || coalesce(' · ' || p.days_to_award || ' days after the tender ' || p.tender_publication_number, '')
             || coalesce(' · ' || trim_scale(a.bids_received) || ' bid(s)', ''),
           case when p.consortium and a.co_winners > 1 then 'Consortium: won jointly with '
             || (select string_agg(w ->> 'name', ', ') from jsonb_array_elements(a.notice_winners) w
                 where lower(btrim(w ->> 'name')) <> a.winner_key) end,
           v_source
         ), 2000),
         jsonb_strip_nulls(jsonb_build_object(
           'snapshot_id', p_snapshot, 'layer', 'baseline', 'procedure_id', p.procedure_id,
           'publication_number', a.pub, 'tender_publication_number', p.tender_publication_number,
           'ted_url', 'https://ted.europa.eu/en/notice/-/detail/' || a.pub,
           'buyer', p.buyer_name, 'country', p.buyer_country, 'procedure_type', p.procedure_type, 'legal_basis', p.legal_basis,
           'winner', a.winner_name, 'winner_identifier', a.winner_identifier, 'winner_country', a.winner_country,
           'winner_size', a.winner_size, 'winner_owner_nationality', a.winner_owner_nationality, 'supplier_id', a.supplier_id,
           'class_id', p.class_id, 'family_id', p.family_id, 'calibre', p.calibre,
           'classification_method', p.classification_method, 'confidence', round(p.classification_confidence::numeric, 2),
           'notice_value', a.notice_value, 'currency', a.currency, 'value_basis', 'notice_total',
           'co_winners', a.co_winners, 'decision_date', a.decision_date, 'bids_received', a.bids_received,
           'days_to_award', p.days_to_award, 'consortium', p.consortium
         )),
         a.pub, a.supplier_id, public.product_bridge_match_buyer(p.buyer_identifier, p.buyer_name, p.buyer_country),
         p.class_id, p.family_id, p.calibre, p.cpv_codes, public.kb_country(p.buyer_country),
         coalesce(a.decision_date, a.published)
  from pg_temp.kb_bc_p p
  cross join lateral (
    select distinct on (lower(btrim(w ->> 'name')))
           'award:' || n.publication_number || '/' || lower(btrim(w ->> 'name')) as key,
           n.publication_number as pub, n.publication_date as published,
           lower(btrim(w ->> 'name')) as winner_key, btrim(w ->> 'name') as winner_name,
           w ->> 'identifier' as winner_identifier, w ->> 'country' as winner_country, w ->> 'size' as winner_size,
           w ->> 'owner_nationality' as winner_owner_nationality,
           n.result_value as notice_value, n.result_currency as currency, n.decision_date, n.bids_received,
           jsonb_array_length(n.winners) as co_winners, n.winners as notice_winners,
           public.product_bridge_match_supplier(w ->> 'identifier', w ->> 'name', w ->> 'country') as supplier_id
    from public.kb_baseline_notices n, jsonb_array_elements(n.winners) w
    where n.snapshot_id = coalesce(v_parent, p_snapshot) and n.publication_number = any (p.result_publication_numbers)
      and nullif(btrim(w ->> 'name'), '') is not null
      and not exists (select 1 from pg_temp.kb_bc_l l where l.publication_number = n.publication_number)
    order by lower(btrim(w ->> 'name')), n.publication_date desc, n.publication_number desc
  ) a;

  -- award cards B: result notices with parsed lots, one card per winner (winners paired through the XML)
  insert into pg_temp.kb_bc
  with e as (
    select l.procedure_id, l.publication_number as pub, l.lot_key, coalesce(l.lot_id, l.lot_key) as lot,
           l.title, l.class_id, l.family_id, l.calibre, l.quantity_used, l.quantity_used_unit, l.unit_price, l.framework,
           l.bids_received, coalesce(l.award_date, l.contract_date) as decided, l.currency as lot_currency,
           lower(btrim(w ->> 'name')) as wkey, btrim(w ->> 'name') as wname, nullif(w ->> 'identifier', '') as wid,
           w ->> 'country' as wcountry, w ->> 'size' as wsize, (w ->> 'sme')::boolean as wsme,
           coalesce((w ->> 'group')::boolean, false) as wgroup,
           coalesce((w ->> 'tender_value')::numeric, case when l.n_winning_tenders = 1 then l.value end) as wvalue,
           coalesce(nullif(w ->> 'tender_currency', ''), l.currency) as wcur,
           (select string_agg(distinct btrim(o ->> 'name'), ', ') from jsonb_array_elements(l.winners) o
             where coalesce((o ->> 'group')::boolean, false) and lower(btrim(o ->> 'name')) <> lower(btrim(w ->> 'name'))) as co
    from pg_temp.kb_bc_l l
    cross join lateral jsonb_array_elements(l.winners) w
    where l.awarded and nullif(btrim(w ->> 'name'), '') is not null
  ),
  g as (
    select e.procedure_id, e.pub, e.wkey,
           (array_agg(e.wname order by e.lot_key))[1] as wname, max(e.wid) as wid, max(e.wcountry) as wcountry,
           max(e.wsize) as wsize, bool_or(e.wsme) as wsme, bool_or(e.wgroup) as wgroup, max(e.co) as co,
           count(*) as n_lots,
           case when count(distinct e.wcur) filter (where e.wvalue is not null) = 1 and count(*) filter (where e.wvalue is null) = 0
                then sum(e.wvalue) end as vwon,
           max(e.wcur) filter (where e.wvalue is not null) as vcur,
           max(e.decided) as decided, max(e.bids_received) as bids,
           case when count(distinct e.family_id) = 1 and count(*) filter (where e.family_id is null) = 0 then max(e.family_id) end as fam,
           case when count(distinct e.class_id) = 1 and count(*) filter (where e.class_id is null) = 0 then max(e.class_id) end as cls,
           case when count(distinct e.calibre) = 1 and count(*) filter (where e.calibre is null) = 0 then max(e.calibre) end as cal,
           string_agg('- ' || e.lot || coalesce(' ' || left(btrim(e.title), 80), '')
                      || coalesce(' · ' || trim_scale(e.quantity_used) || ' '
                                  || case e.quantity_used_unit when 'H87' then 'pieces' else coalesce(e.quantity_used_unit, '') end, '')
                      || coalesce(' · ' || public.kb_fmt_money(e.wvalue, e.wcur), '')
                      || coalesce(' (' || trim_scale(round(e.unit_price, 4)) || ' ' || e.lot_currency || ' per piece)', '')
                      || case when e.framework then ' · framework agreement' else '' end,
                      E'\n' order by e.lot_key) as lots_text,
           jsonb_agg(jsonb_strip_nulls(jsonb_build_object('lot', e.lot, 'title', left(e.title, 120), 'family_id', e.family_id,
                       'calibre', e.calibre, 'quantity', e.quantity_used, 'unit', e.quantity_used_unit, 'value', e.wvalue,
                       'currency', e.wcur, 'unit_price', e.unit_price, 'framework', e.framework)) order by e.lot_key) as lots
    from e group by e.procedure_id, e.pub, e.wkey
  )
  select 'award', 'award:' || g.pub || '/' || g.wkey,
         left(g.wname || ' won ' || g.pub || ' · ' || coalesce(nullif(btrim(p.title_en), ''), nullif(btrim(p.title_original), ''), 'award'), 300),
         left(concat_ws(E'\n',
           'AWARD ' || g.pub || ' · ' || coalesce(p.buyer_country, 'country not stated') || ' · buyer '
             || coalesce(nullif(btrim(p.buyer_name), ''), 'not stated') || ' → winner ' || g.wname
             || ' (' || coalesce(g.wcountry, 'country not stated') || ', '
             || coalesce(g.wsize, case g.wsme when true then 'SME' when false then 'not an SME' end, 'size not stated') || ')',
           'What: ' || public.kb_what(coalesce(g.fam, p.family_id), coalesce(g.cls, p.class_id), coalesce(g.cal, p.calibre)),
           coalesce(nullif(btrim(p.title_en), ''), nullif(btrim(p.title_original), '')),
           'Lots won (from the notice XML; ' || g.n_lots || ' lot(s)):' || E'\n' || left(g.lots_text, 900),
           'Value won: ' || coalesce(public.kb_fmt_money(g.vwon, g.vcur)
                                     || case when g.wgroup then ' (joint tender of the consortium)' else '' end, 'not published'),
           'Decided: ' || coalesce(g.decided::text, 'not stated')
             || coalesce(' · ' || p.days_to_award || ' days after the tender ' || p.tender_publication_number, '')
             || coalesce(' · ' || g.bids || ' bid(s)', ''),
           case when g.co is not null then 'Consortium: won jointly with ' || g.co end,
           v_source
         ), 2000),
         jsonb_strip_nulls(jsonb_build_object(
           'snapshot_id', p_snapshot, 'layer', 'baseline', 'procedure_id', p.procedure_id,
           'publication_number', g.pub, 'tender_publication_number', p.tender_publication_number,
           'ted_url', 'https://ted.europa.eu/en/notice/-/detail/' || g.pub,
           'buyer', p.buyer_name, 'country', p.buyer_country, 'procedure_type', p.procedure_type, 'legal_basis', p.legal_basis,
           'winner', g.wname, 'winner_identifier', g.wid, 'winner_country', g.wcountry, 'winner_size', g.wsize, 'winner_sme', g.wsme,
           'supplier_id', s.supplier_id,
           'class_id', coalesce(g.cls, p.class_id), 'family_id', coalesce(g.fam, p.family_id), 'calibre', coalesce(g.cal, p.calibre),
           'classification_method', case when g.fam is not null then 'lots (rules-v1)' else p.classification_method end,
           'value_won', g.vwon, 'currency', g.vcur, 'value_basis', 'lot_values_xml', 'lots_won', g.n_lots, 'lots', g.lots,
           'decision_date', g.decided, 'bids_received', g.bids, 'days_to_award', p.days_to_award,
           'consortium', g.wgroup, 'co_winners', g.co
         )),
         g.pub, s.supplier_id, public.product_bridge_match_buyer(p.buyer_identifier, p.buyer_name, p.buyer_country),
         coalesce(g.cls, p.class_id), coalesce(g.fam, p.family_id), coalesce(g.cal, p.calibre), p.cpv_codes,
         public.kb_country(p.buyer_country), coalesce(g.decided, p.decided, p.published)
  from g
  join pg_temp.kb_bc_p p on p.procedure_id = g.procedure_id
  cross join lateral (select public.product_bridge_match_supplier(g.wid, g.wname, g.wcountry) as supplier_id) s;

  -- pattern cards
  insert into pg_temp.kb_bc
  select 'pattern', 'pattern:' || t.snapshot_id || '/' || t.id,
         left('Pattern ' || t.id || ' (' || t.kind || '): ' || t.statement, 300),
         left(concat_ws(E'\n',
           'PATTERN ' || t.id || ' · ' || t.kind || ' · ' || t.snapshot_id,
           'Scope: ' || coalesce(public.kb_jsonb_text(t.scope, 300), 'all families and countries'),
           t.statement,
           'Support: ' || t.support,
           case when cardinality(t.examples) > 0 then 'Examples: ' || array_to_string(t.examples, ', ') end,
           v_source
         ), 2000),
         jsonb_build_object('snapshot_id', t.snapshot_id, 'layer', 'baseline', 'pattern_id', t.id, 'kind', t.kind,
                            'scope', t.scope, 'support', t.support, 'examples', to_jsonb(t.examples), 'metrics', t.metrics),
         null, null, null, f.parent_id, case when f.level = 2 then f.id end, null, '{}'::text[],
         public.kb_country(t.scope ->> 'country'), v_cutoff
  from public.kb_patterns t
  left join public.product_families f on f.id = t.scope ->> 'family_id'
  where t.snapshot_id = p_snapshot;

  select jsonb_object_agg(z.kind, z.n) into v_built from (select c.kind, count(*) n from pg_temp.kb_bc c group by 1) z;

  insert into public.kb_chunks (kind, source_key, ref, title, content, facts, publication_number, supplier_id, buyer_id,
                                item_ids, class_id, family_id, calibre, cpv_codes, country, occurred_at, layer, snapshot_id,
                                visibility, party_ids, embedding, embedding_model, embedded_hash, embedded_at)
  select c.kind, c.source_key, c.source_key,
         case when d.same then o.title else c.title end,
         case when d.same then o.content else c.content end,
         case when d.same then c.facts || jsonb_build_object('card_text_from', v_parent) else c.facts end,
         c.publication_number, c.supplier_id, c.buyer_id, '{}'::uuid[], c.class_id, c.family_id, c.calibre,
         coalesce(c.cpv_codes, '{}'), c.country, c.occurred_at, 'baseline', p_snapshot, 'public', '{}'::text[],
         case when d.same then o.embedding end, case when d.same then o.embedding_model end,
         case when d.same then o.embedded_hash end, case when d.same then o.embedded_at end
  from pg_temp.kb_bc c
  left join public.kb_chunks o
    on v_parent is not null and o.kind = c.kind and o.source_key = c.source_key and o.layer = 'baseline' and o.snapshot_id = v_parent
  cross join lateral (
    select o.id is not null and o.title = c.title and o.content = replace(c.content, v_source, v_parent_source) as same
  ) d
  where v_parent is null or c.kind = 'pattern' or not d.same
     or (c.kind = 'tender' and exists (select 1 from public.ted_notices n
                                       where n.kind = 'competition' and 'ted:' || n.publication_number = c.source_key))
     or (c.kind = 'award' and exists (select 1 from public.ted_awards a
                                      where 'award:' || a.publication_number || '/' || a.winner_key = c.source_key));

  select jsonb_build_object(
           'by_kind', (select jsonb_object_agg(z.kind, z.n) from (select k.kind, count(*) n from public.kb_chunks k
                        where k.layer = 'baseline' and k.snapshot_id = p_snapshot group by 1) z),
           'copied_with_embedding', (select count(*) from public.kb_chunks k
                        where k.layer = 'baseline' and k.snapshot_id = p_snapshot and k.embedding is not null),
           'to_embed', (select count(*) from public.kb_chunks k
                        where k.layer = 'baseline' and k.snapshot_id = p_snapshot and k.embedding is null))
    into v_written;

  return jsonb_build_object('built', v_built, 'written', v_written,
    'inherited_from_parent', case when v_parent is not null then
      (select count(*) from public.kb_chunks k where k.layer = 'baseline' and k.snapshot_id = v_parent
         and public.kb_baseline_inherited(p_snapshot, k.kind, k.source_key, k.publication_number)) end);
end
$$;
revoke all on function public.kb_baseline_build_chunks(text) from public, anon, authenticated;
grant execute on function public.kb_baseline_build_chunks(text) to service_role;

-- ---------------------------------------------------------------------------------------------- hash and freeze
-- Snapshot without parent: unchanged (notices). Overlay snapshot: sha256 of "parent:<parent content_sha256>" and
-- the ordered "publication_number/lot_key:md5(parsed XML columns)" of its kb_baseline_lots.
create or replace function public.kb_baseline_content_hash(p_snapshot text)
returns text
language sql
stable
security definer
set search_path = ''
as $$
  select case
    when s.parent_id is null then
      (select encode(sha256(convert_to(coalesce(string_agg(n.publication_number || ':' || md5(n.raw::text), E'\n'
                                                           order by n.publication_number collate "C"), ''), 'UTF8')), 'hex')
       from public.kb_baseline_notices n
       where n.snapshot_id = p_snapshot)
    else
      (select encode(sha256(convert_to('parent:' || coalesce(ps.content_sha256, '') || E'\n'
                || coalesce(string_agg(l.publication_number || '/' || l.lot_key || ':'
                                       || md5((to_jsonb(l) - array['inserted_at', 'procedure_id', 'class_id', 'family_id', 'calibre',
                                               'classification_method', 'classification_confidence', 'classification_input',
                                               'classification_detail', 'unit_price', 'unit_price_basis', 'unit_price_excluded',
                                               'quantity_used', 'quantity_used_unit', 'quantity_source'])::text),
                                       E'\n' order by l.publication_number collate "C", l.lot_key collate "C"), ''), 'UTF8')), 'hex')
       from public.kb_baseline_lots l
       where l.snapshot_id = p_snapshot)
  end
  from public.kb_snapshots s
  left join public.kb_snapshots ps on ps.id = s.parent_id
  where s.id = p_snapshot
$$;
revoke all on function public.kb_baseline_content_hash(text) from public, anon, authenticated;
grant execute on function public.kb_baseline_content_hash(text) to service_role;

create or replace function public.kb_baseline_freeze(p_snapshot text, p_activate boolean default true)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  s public.kb_snapshots%rowtype;
  v_counts jsonb;
  v_manifest jsonb;
  v_hash text;
  v_tax int;
  v_focus int;
  v_xml int;
begin
  select * into s from public.kb_snapshots where id = p_snapshot for update;
  if not found then
    raise exception 'kb_baseline_freeze: unknown snapshot %', p_snapshot;
  end if;
  if s.status <> 'building' then
    raise exception 'kb_baseline_freeze: snapshot % is already %', p_snapshot, s.status;
  end if;
  if coalesce((s.harvest_state ->> 'done')::boolean, (s.harvest_state -> 'lots' ->> 'done')::boolean, false) is not true then
    raise exception 'kb_baseline_freeze: harvest of % is not done', p_snapshot;
  end if;

  v_hash := public.kb_baseline_content_hash(p_snapshot);
  select max(t.version) into v_tax from public.product_taxonomy_versions t;

  if s.parent_id is null then
    v_counts := jsonb_build_object(
      'notices_per_query', (select jsonb_object_agg(q.q, q.n) from (
          select x.q, count(*) as n from public.kb_baseline_notices n, unnest(n.queries) x(q)
          where n.snapshot_id = p_snapshot group by x.q) q),
      'ted_totals_per_query', (select jsonb_object_agg(e.key, e.value -> 'total') from jsonb_each(s.harvest_state -> 'main') e),
      'kb_baseline_notices', (select jsonb_build_object('total', count(*),
          'competition', count(*) filter (where n.kind = 'competition'),
          'result', count(*) filter (where n.kind = 'result'),
          'modification', count(*) filter (where n.kind = 'modification'),
          'eforms', count(*) filter (where n.notice_identifier is not null))
          from public.kb_baseline_notices n where n.snapshot_id = p_snapshot),
      'kb_baseline_procedures', (select jsonb_build_object('total', sum(z.n), 'by_outcome', jsonb_object_agg(z.outcome, z.n))
          from (select p.outcome, count(*) as n from public.kb_baseline_procedures p where p.snapshot_id = p_snapshot group by p.outcome) z),
      'procedures_by_link_method', (select jsonb_object_agg(coalesce(z.link_method, 'none'), z.n)
          from (select p.link_method, count(*) as n from public.kb_baseline_procedures p where p.snapshot_id = p_snapshot group by p.link_method) z),
      'procedures_by_classification', (select jsonb_object_agg(coalesce(z.m, 'none'), z.n)
          from (select p.classification_method as m, count(*) as n from public.kb_baseline_procedures p where p.snapshot_id = p_snapshot group by 1) z),
      'kb_baseline_stats', (select jsonb_object_agg(z.scope_level, z.n)
          from (select t.scope_level, count(*) as n from public.kb_baseline_stats t where t.snapshot_id = p_snapshot group by 1) z),
      'kb_patterns', (select jsonb_object_agg(z.kind, z.n)
          from (select t.kind, count(*) as n from public.kb_patterns t where t.snapshot_id = p_snapshot group by 1) z),
      'kb_chunks', (select jsonb_object_agg(z.kind, z.n)
          from (select k.kind, count(*) as n from public.kb_chunks k where k.layer = 'baseline' and k.snapshot_id = p_snapshot group by 1) z),
      'ted_requests', s.harvest_state -> 'requests',
      'harvest_runs', s.harvest_state -> 'runs');

    v_manifest := jsonb_build_object(
      'snapshot_id', p_snapshot,
      'cutoff_date', s.cutoff_date,
      'rule', 'TED notices with PD <= cutoff; only procedures closed at the cutoff (tenders still open stay in ted_live)',
      'queries', s.queries,
      'counts', v_counts,
      'taxonomy_version', v_tax,
      'classifier', 'ontology rules-v1 (CPV + calibre + keyword stages) via the read-only port kb_baseline_classify; no LLM stage',
      'content_sha256', v_hash,
      'content_hash_method', 'sha256 of newline-joined "publication_number:md5(raw::text)" ordered by publication_number (collate C); kb_baseline_content_hash()',
      'harvest', jsonb_build_object('started_at', s.harvest_state -> 'started_at', 'finished_at', s.harvest_state -> 'finished_at',
                                    'edge_function', 'kb-baseline-harvest'),
      'file', null);
  else
    select count(distinct r.pub) into v_focus
    from public.kb_baseline_procedures p, unnest(p.result_publication_numbers) r(pub)
    where p.snapshot_id = s.parent_id and p.class_id in ('ammunition', 'firearms', 'parts', 'misc_weapons');
    select count(distinct l.publication_number) into v_xml from public.kb_baseline_lots l where l.snapshot_id = p_snapshot;

    v_counts := jsonb_build_object(
      'parent_id', s.parent_id,
      'coverage', jsonb_build_object(
        'focus_result_notices', v_focus, 'result_notices_with_xml_lots', v_xml,
        'share', round(v_xml::numeric / nullif(v_focus, 0), 4),
        'text', format('PARTIAL: XML lot data for %s of %s result notices in the four focus classes', v_xml, v_focus)),
      'kb_baseline_lots', (select jsonb_build_object('total', count(*), 'notices', count(distinct l.publication_number),
          'procedures', count(distinct l.procedure_id),
          'eforms', count(*) filter (where l.format = 'eforms'), 'pre_eforms', count(*) filter (where l.format <> 'eforms'),
          'awarded', count(*) filter (where l.awarded), 'with_winner', count(*) filter (where jsonb_array_length(l.winners) > 0),
          'pre_eforms_with_winner', count(*) filter (where l.format <> 'eforms' and jsonb_array_length(l.winners) > 0),
          'with_value', count(*) filter (where l.value is not null),
          'with_quantity_xml', count(*) filter (where l.quantity is not null),
          'with_quantity_used', count(*) filter (where l.quantity_used is not null),
          'with_unit_price', count(*) filter (where l.unit_price is not null),
          'with_family', count(*) filter (where l.family_id is not null),
          'with_calibre', count(*) filter (where l.calibre is not null))
          from public.kb_baseline_lots l where l.snapshot_id = p_snapshot),
      'kb_baseline_procedures', jsonb_build_object(
          'overrides', (select count(*) from public.kb_baseline_procedures p where p.snapshot_id = p_snapshot),
          'resolved_total', (select count(*) from public.kb_baseline_procedures_of(p_snapshot)),
          'resolved_by_outcome', (select jsonb_object_agg(z.outcome, z.n) from (select p.outcome, count(*) n
                                   from public.kb_baseline_procedures_of(p_snapshot) p group by 1) z)),
      'unit_price_points', (select jsonb_object_agg(z.source, z.n) from (select u.source, count(*) n
          from public.kb_baseline_unit_price_points u where u.snapshot_id = p_snapshot group by 1) z),
      'kb_baseline_stats', (select jsonb_object_agg(z.scope_level, z.n)
          from (select t.scope_level, count(*) as n from public.kb_baseline_stats t where t.snapshot_id = p_snapshot group by 1) z),
      'kb_patterns', (select jsonb_object_agg(z.kind, z.n)
          from (select t.kind, count(*) as n from public.kb_patterns t where t.snapshot_id = p_snapshot group by 1) z),
      'kb_chunks_own', (select jsonb_object_agg(z.kind, z.n)
          from (select k.kind, count(*) as n from public.kb_chunks k where k.layer = 'baseline' and k.snapshot_id = p_snapshot group by 1) z),
      'kb_chunks_inherited', (select count(*) from public.kb_chunks k where k.layer = 'baseline' and k.snapshot_id = s.parent_id
                                and public.kb_baseline_inherited(p_snapshot, k.kind, k.source_key, k.publication_number)),
      'xml_requests_edge', s.harvest_state -> 'lots' -> 'requests',
      'xml_runs_edge', s.harvest_state -> 'lots' -> 'runs');

    v_manifest := jsonb_build_object(
      'snapshot_id', p_snapshot,
      'parent_id', s.parent_id,
      'parent_content_sha256', (select ps.content_sha256 from public.kb_snapshots ps where ps.id = s.parent_id),
      'cutoff_date', s.cutoff_date,
      'rule', 'Overlay on the parent snapshot: the parent''s notices and procedures plus lot-level data read from the result-notice XML of a sample of the four focus classes; where there is no lot data, the parent''s numbers stay.',
      'queries', s.queries,
      'counts', v_counts,
      'notes', s.notes,
      'taxonomy_version', v_tax,
      'classifier', 'ontology rules-v1 via the read-only port kb_baseline_classify, per lot (lot title + description; procedure text for context); no LLM stage',
      'unit_price_rule', 'lot value (BT-720, or BT-161 when the notice awards only this lot) / stated quantity (BT-25) in pieces (H87), quantity > 1, one confirmed winning tender, no framework agreement or maximum; native currency; no text parsing',
      'content_sha256', v_hash,
      'content_hash_method', 'sha256 of "parent:<parent content_sha256>" + newline-joined "publication_number/lot_key:md5(parsed XML columns of kb_baseline_lots)" ordered (collate C); kb_baseline_content_hash()',
      'harvest', jsonb_build_object('sample_file', s.harvest_state -> 'lots' -> 'sample_file',
                                    'local_fetch', s.harvest_state -> 'lots' -> 'local_fetch',
                                    'edge_function', 'kb-baseline-lots',
                                    'started_at', s.harvest_state -> 'lots' -> 'started_at',
                                    'finished_at', s.harvest_state -> 'lots' -> 'finished_at'),
      'file', null);
  end if;

  if p_activate then
    update public.kb_snapshots set active = false where active and id <> p_snapshot;
  end if;

  update public.kb_snapshots set
    status = 'frozen',
    frozen_at = now(),
    active = p_activate,
    counts = v_counts,
    manifest = v_manifest,
    content_sha256 = v_hash,
    taxonomy_version = v_tax
  where id = p_snapshot;

  return v_manifest;
end
$$;
revoke all on function public.kb_baseline_freeze(text, boolean) from public, anon, authenticated;
grant execute on function public.kb_baseline_freeze(text, boolean) to service_role;
