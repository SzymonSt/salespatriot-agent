-- WS8 baseline_v2, part 1: lot-level data from the notice XML, as an overlay on the frozen baseline_v1.
--
-- baseline_v2 does not copy v1: kb_snapshots.parent_id = 'baseline_v1', the notices stay in v1 (no raw XML or
-- raw jsonb is stored), and v2 adds
--   kb_baseline_lots        one row per lot result parsed from a result notice's XML (ted_baseline_lots.py);
--   kb_baseline_procedures  v2 rows only for procedures whose data changed with the lots (overrides of v1 rows;
--                           kb_baseline_procedures_resolved gives the full set);
--   kb_baseline_stats, kb_patterns  complete for v2 (rebuilt over the resolved procedures);
--   kb_chunks (baseline)    v2 rows only for cards that changed, plus v1 cards whose ref also exists in the live
--                           TED tables; the other v1 cards stay visible through the parent (kb_baseline_inherited).
--
-- Partial coverage: XML lot data for a stratified sample of the 2,580 result notices in the four focus classes
-- (data/kb_baseline_v2_sample.csv); where v2 has no lot data, the v1 numbers stay.

alter table public.kb_snapshots add column if not exists parent_id text references public.kb_snapshots (id);

create table if not exists public.kb_baseline_lots (
  snapshot_id text not null references public.kb_snapshots (id),
  publication_number text not null,
  lot_key text not null,
  procedure_id text,
  format text not null check (format in ('eforms', 'legacy_2014', 'legacy_r208')),
  lot_id text,
  n_lots_in_notice int,
  title text,
  description text,
  cpv_codes text[] not null default '{}',
  quantity numeric,
  quantity_unit text,
  estimated_value numeric,
  estimated_currency text,
  framework boolean not null default false,
  framework_max numeric,
  framework_currency text,
  result_code text,
  awarded boolean not null default false,
  winner_confirmed boolean,
  n_winning_tenders int not null default 0,
  winners jsonb not null default '[]',
  value numeric,
  currency text,
  value_source text,
  bids_received int,
  award_date date,
  contract_date date,
  class_id text,
  family_id text,
  calibre text,
  classification_method text,
  classification_confidence real,
  classification_input text,
  classification_detail jsonb,
  unit_price numeric,
  unit_price_basis text,
  unit_price_excluded text,
  inserted_at timestamptz not null default now(),
  primary key (snapshot_id, publication_number, lot_key)
);
comment on table public.kb_baseline_lots is 'baseline_v2+: one row per lot result parsed from TED result-notice XML (ted_baseline_lots.py). quantity = eForms BT-25 only; value = the lot''s own value (value_source); framework_max is a ceiling, never a price; unit_price strict (kb_baseline_lots_classify).';
create index if not exists kb_baseline_lots_procedure on public.kb_baseline_lots (snapshot_id, procedure_id);
create index if not exists kb_baseline_lots_family on public.kb_baseline_lots (snapshot_id, family_id, calibre);

drop trigger if exists kb_baseline_frozen_guard on public.kb_baseline_lots;
create trigger kb_baseline_frozen_guard before insert or update or delete on public.kb_baseline_lots
  for each row execute function public.kb_baseline_frozen_guard();
drop trigger if exists kb_baseline_frozen_guard_truncate on public.kb_baseline_lots;
create trigger kb_baseline_frozen_guard_truncate before truncate on public.kb_baseline_lots
  for each statement execute function public.kb_baseline_frozen_guard();

alter table public.kb_baseline_lots enable row level security;
drop policy if exists "public read" on public.kb_baseline_lots;
create policy "public read" on public.kb_baseline_lots for select to anon, authenticated using (true);
revoke insert, update, delete, truncate on public.kb_baseline_lots from anon, authenticated;

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

  update public.kb_baseline_lots l set
    unit_price_excluded = x.reason,
    unit_price = case when x.reason is null then round(l.value / l.quantity, 4) end,
    unit_price_basis = case when x.reason is null then
      'lot value (' || l.value_source || ') / stated lot quantity (BT-25, ' || l.quantity_unit || ')' end
  from (
    select z.publication_number, z.lot_key,
           case
             when z.format <> 'eforms' then 'no structured quantity (pre-eForms notice)'
             when not z.awarded then 'lot not awarded'
             when z.quantity is null then 'no stated quantity'
             when z.quantity_unit is distinct from 'H87' then 'quantity not in pieces (' || coalesce(z.quantity_unit, 'no unit') || ')'
             when z.quantity <= 1 then 'quantity 1 (lump sum)'
             when z.framework or z.framework_max is not null then 'framework agreement'
             when z.n_winning_tenders <> 1 then 'several winning tenders'
             when z.winner_confirmed is not true then 'winner not confirmed'
             when z.value is null or z.value <= 0 then 'no lot value'
             when z.value_source not in ('tender_value', 'notice_total_one_lot') then 'value is not the lot''s own'
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
    'with_unit_price', (select count(*) from public.kb_baseline_lots l where l.snapshot_id = p_snapshot and l.unit_price is not null),
    'unit_price_excluded', (select jsonb_object_agg(z.r, z.n) from (select l.unit_price_excluded r, count(*) n
                  from public.kb_baseline_lots l where l.snapshot_id = p_snapshot and l.unit_price_excluded is not null group by 1) z));
end
$$;
revoke all on function public.kb_baseline_lots_classify(text) from public, anon, authenticated;
grant execute on function public.kb_baseline_lots_classify(text) to service_role;

insert into public.kb_snapshots (id, cutoff_date, status, parent_id, queries, notes)
values (
  'baseline_v2', '2026-10-30', 'building', 'baseline_v1',
  jsonb_build_array(
    jsonb_build_object('key', 'xml_result_sample', 'kind', 'result', 'source', 'https://ted.europa.eu/en/notice/<publication-number>/xml',
      'query', 'Result notices (can-standard, can-desg, can-social, can-tran) of baseline_v1 procedures classified in the four focus classes (ammunition, firearms, parts, misc_weapons): 2,580 notices. Sample of 250 + 5 already cached, listed in data/kb_baseline_v2_sample.csv.',
      'selection', 'Stratified by class in proportion to result notices (ammunition 156, firearms 45, parts 40, misc_weapons 9), each about half pre-eForms / half eForms; within a stratum ranked by 4 x calibre known (procedure or title) + 2 x buyer has an open tender + 1 x buyer country has an open tender + 1 x family in open tenders (ted_open_tenders, 2026-10-30), then newest first.')
  ),
  'PARTIAL COVERAGE: XML lot data for a sample of the 2,580 result notices in the four focus classes (see counts.coverage). Overlay on baseline_v1 (parent_id): v1 notices are not copied; v2 holds kb_baseline_lots, procedure rows that changed, complete stats and patterns, and the baseline cards that changed. Where v2 has no lot data, the v1 numbers stay.'
)
on conflict (id) do nothing;
