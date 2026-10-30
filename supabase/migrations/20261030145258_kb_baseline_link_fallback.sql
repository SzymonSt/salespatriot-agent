-- kb_baseline_link v2: adds the optional fallback grouping of WS8 8.4 for results without any tender link
-- (legacy notices have no procedure id; eForms results of legacy tenders get a new one): the single tender-only
-- procedure of the same buyer (normalised name) and country, with an overlapping CPV, published in the 18
-- months before the result. Ambiguous candidates stay unlinked. link_method = 'buyer_cpv_window'.
-- Legacy tenders that stay without a linked result say so in outcome_basis instead of implying no award.

create or replace function public.kb_baseline_link(p_snapshot text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_cutoff date;
  v_status text;
  v_rows integer;
begin
  select s.cutoff_date, s.status into v_cutoff, v_status from public.kb_snapshots s where s.id = p_snapshot;
  if v_status is distinct from 'building' then
    raise exception 'kb_baseline_link: snapshot % is % (must be building)', p_snapshot, coalesce(v_status, 'missing');
  end if;

  delete from public.kb_baseline_procedures p where p.snapshot_id = p_snapshot;

  insert into public.kb_baseline_procedures (
    snapshot_id, procedure_id, link_method, eforms, tender_publication_number, competition_publication_numbers,
    result_publication_numbers, modification_publication_numbers, outcome, result_status, outcome_basis, overdue,
    legal_basis, procedure_type, buyer_name, buyer_identifier, buyer_country, buyer_legal_type, main_activity,
    title_en, title_original, description, cpv_codes, published, deadline, decided, decided_basis, days_to_award,
    estimated_value, estimated_currency, awarded_value, currency, n_lots, quantity, quantity_unit, unit_price,
    bids_received, winners, consortium, non_award_reasons)
  with n as materialized (
    select x.* from public.kb_baseline_notices x where x.snapshot_id = p_snapshot
  ),
  -- tender publication numbers referenced by results (previous-notice-id-proc); the first referencing result wins
  prev as (
    select distinct on (ltrim(p.x, '0')) ltrim(p.x, '0') as pub, coalesce(r.procedure_id, 'pub:' || r.publication_number) as gkey
    from n r, unnest(r.previous_notice_ids) as p(x)
    where r.kind in ('result', 'modification') and p.x ~ '^[0-9]{1,8}-[0-9]{4}$'
    order by ltrim(p.x, '0'), r.publication_date, r.publication_number
  ),
  with_results as (
    select distinct r.procedure_id from n r where r.kind in ('result', 'modification') and r.procedure_id is not null
  ),
  -- an eForms tender whose own procedure id has no result, referenced by a result of another procedure
  remap as (
    select distinct on (c.procedure_id) c.procedure_id as from_key, pv.gkey as to_key
    from n c
    join prev pv on pv.pub = c.publication_number
    left join with_results wr on wr.procedure_id = c.procedure_id
    where c.kind = 'competition' and c.procedure_id is not null and c.procedure_id <> pv.gkey and wr.procedure_id is null
    order by c.procedure_id, pv.gkey
  ),
  keyed0 as materialized (
    select n.*,
           case when n.procedure_id is not null then coalesce(rm.to_key, n.procedure_id)
                when n.kind = 'competition' and pv.gkey is not null then pv.gkey
                else 'pub:' || n.publication_number end as gkey,
           (rm.to_key is not null or (n.procedure_id is null and n.kind = 'competition' and pv.gkey is not null)) as via_previous,
           -- eForms change notices republish a result under a new number: keep the latest version only
           case when n.kind = 'result' and n.notice_identifier is not null
                then row_number() over (partition by n.kind, n.notice_identifier
                                        order by coalesce(n.notice_version, 0) desc, n.publication_date desc,
                                                 n.publication_number desc) > 1
                else false end as superseded
    from n
    left join remap rm on rm.from_key = n.procedure_id
    left join prev pv on pv.pub = n.publication_number and n.kind = 'competition'
  ),
  -- fallback for results without any tender link (legacy notices, or eForms results whose tender was a
  -- legacy notice): the only tender-only procedure of the same buyer (normalised name) and country, with
  -- an overlapping CPV, published in the 18 months before the result. Ambiguous matches stay unlinked.
  g0 as (
    select k.gkey,
           bool_or(k.kind = 'competition') as has_comp,
           bool_or(k.kind in ('result', 'modification')) as has_res,
           min(k.publication_date) filter (where k.kind in ('result', 'modification')) as res_date,
           min(k.publication_date) filter (where k.kind = 'competition') as comp_date,
           max(lower(regexp_replace(coalesce(k.buyer_name, ''), '[^[:alnum:]]+', '', 'g'))) as bk,
           max(k.buyer_country) as country,
           array_agg(distinct c) as cpv
    from keyed0 k, unnest(case when cardinality(k.cpv_codes) > 0 then k.cpv_codes else array[null::text] end) c
    group by k.gkey
  ),
  fb as (
    select r.gkey as r_key, min(c.gkey) as c_key
    from g0 r
    join g0 c on c.has_comp and not c.has_res and c.bk = r.bk and c.country = r.country
             and c.comp_date between (r.res_date - interval '18 months')::date and r.res_date
             and c.cpv && r.cpv
    where r.has_res and not r.has_comp and r.bk <> ''
    group by r.gkey
    having count(*) = 1
  ),
  keyed as materialized (
    select k0.*, coalesce(fb.c_key, k0.gkey) as gkey_final, (fb.c_key is not null) as via_fallback
    from keyed0 k0 left join fb on fb.r_key = k0.gkey
  ),
  res as materialized (
    select k.*, k.gkey_final as gk,
           (jsonb_array_length(k.winners) > 0 or 'selec-w' = any (k.winner_selection_status)
            or (cardinality(k.winner_selection_status) = 0 and coalesce(k.result_value, 0) > 0)) as awarded
    from keyed k where k.kind = 'result' and not k.superseded
  ),
  cur as (                                             -- dominant result currency per procedure
    select distinct on (r.gk) r.gk as gkey, r.result_currency as currency
    from res r where r.result_value is not null and r.result_currency is not null
    group by r.gk, r.result_currency
    order by r.gk, count(*) desc, r.result_currency
  ),
  win as (
    select w.gkey, jsonb_agg(w.winner order by lower(w.winner ->> 'name')) as winners
    from (select distinct on (r.gk, lower(e ->> 'name')) r.gk as gkey, e as winner
          from res r, jsonb_array_elements(r.winners) e
          order by r.gk, lower(e ->> 'name'), r.publication_date) w
    group by w.gkey
  ),
  cpvs as (
    select k.gkey_final as gkey, array_agg(distinct c order by c) as cpv_codes
    from keyed k, unnest(k.cpv_codes) c
    group by k.gkey_final
  ),
  reasons as (
    select r.gk as gkey, array_agg(distinct j order by j) as reasons
    from res r, unnest(r.non_award_justification) j
    group by r.gk
  ),
  g as (
    select k.gkey_final as gkey,
           bool_or(k.kind = 'competition') as has_comp,
           bool_or(k.kind = 'result' and not k.superseded) as has_res,
           bool_or(k.kind = 'modification') as has_mod,
           bool_or(k.notice_identifier is not null) as eforms,
           bool_or(k.via_previous) as via_previous,
           bool_or(k.via_fallback) as via_fallback,
           (array_agg(k.publication_number order by (coalesce(k.notice_type, '') like 'pin-%'), k.publication_date, k.publication_number)
              filter (where k.kind = 'competition'))[1] as tender,
           coalesce(array_agg(k.publication_number order by k.publication_date, k.publication_number)
              filter (where k.kind = 'competition'), '{}') as comps,
           coalesce(array_agg(k.publication_number order by k.publication_date, k.publication_number)
              filter (where k.kind = 'result' and not k.superseded), '{}') as results,
           coalesce(array_agg(k.publication_number order by k.publication_date, k.publication_number)
              filter (where k.kind = 'modification'), '{}') as mods,
           max(k.n_lots) as n_lots,
           -- explicit single-lot quantity: tender first, then a result
           (array_agg(k.quantity order by (k.kind <> 'competition'), k.publication_date)
              filter (where k.quantity is not null and not k.superseded))[1] as quantity,
           (array_agg(k.quantity_unit order by (k.kind <> 'competition'), k.publication_date)
              filter (where k.quantity is not null and not k.superseded))[1] as quantity_unit
    from keyed k
    group by k.gkey_final
  ),
  rg as (
    select r.gk as gkey,
           bool_or(r.awarded) as any_awarded,
           bool_or('clos-nw' = any (r.winner_selection_status)) as any_closed,
           bool_or('open-nw' = any (r.winner_selection_status)) as any_open,
           bool_or(jsonb_array_length(r.winners) > 0) as any_winner,
           bool_or('selec-w' = any (r.winner_selection_status)) as any_selected,
           max(r.decision_date) as decision_date,
           max(r.contract_conclusion_date) as conclusion_date,
           max(r.publication_date) as result_published,
           min(r.publication_date) as first_result_published,
           sum(r.result_value) filter (where r.result_currency = c.currency) as awarded_value,
           max(c.currency) as currency,
           max(r.bids_received) as bids_received,
           bool_or(cardinality(r.tendering_parties) > 0
                   and jsonb_array_length(r.winners) > cardinality(r.tendering_parties)) as consortium,
           count(*) as n_results,
           (array_agg(r.publication_number order by r.publication_date, r.publication_number))[1] as first_result
    from res r left join cur c on c.gkey = r.gk
    group by r.gk
  ),
  o as (
    select g.gkey, g.has_comp, g.has_res, g.has_mod, g.eforms, g.via_previous, g.via_fallback, g.tender, g.comps, g.results, g.mods,
           g.n_lots, g.quantity, g.quantity_unit,
           rg.decision_date, rg.conclusion_date, rg.result_published, rg.first_result_published, rg.awarded_value,
           rg.currency, rg.bids_received, rg.consortium, rg.n_results,
           coalesce(rs.reasons, '{}') as reasons,
           coalesce(g.tender, rg.first_result, g.mods[1]) as main_pub,
           case
             when rg.any_awarded then 'awarded'
             when rg.any_closed and (rs.reasons is null or rs.reasons && array['no-rece', 'all-rej']) then 'unsuccessful'
             when rg.any_closed then 'cancelled'
             when rg.any_open then 'pending'
             when g.has_res then 'unknown'
             when g.has_mod then 'awarded'
           end as result_status,
           case
             when rg.any_winner then 'winner'
             when rg.any_selected then 'status:selec-w'
             when rg.any_awarded then 'value'
             when rg.any_closed then 'status:clos-nw' || coalesce(':' || array_to_string(rs.reasons, ','), '')
             when rg.any_open then 'status:open-nw'
             when g.has_res then 'result_notice_without_value_or_status'
             when g.has_mod then 'modification_notice'
             when not g.eforms then 'no_linked_result:legacy_notice_without_procedure_link'
             else 'no_result_notice'
           end as outcome_basis
    from g
    left join rg on rg.gkey = g.gkey
    left join reasons rs on rs.gkey = g.gkey
  )
  select p_snapshot,
         o.gkey,
         case when o.via_previous then 'previous_notice'
              when o.via_fallback then 'buyer_cpv_window'
              when o.gkey like 'pub:%' then 'single_notice'
              else 'procedure_id' end,
         o.eforms,
         o.tender,
         o.comps,
         o.results,
         o.mods,
         case
           when not o.has_comp and (o.has_res or o.has_mod) then 'result_only'
           when not o.has_res and not o.has_mod then 'no_result_yet'
           when o.result_status in ('awarded', 'unsuccessful', 'cancelled') then o.result_status
           when o.result_status = 'pending' then 'no_result_yet'
           else 'awarded'          -- legacy result notice with neither value nor status: see outcome_basis
         end,
         o.result_status,
         o.outcome_basis,
         case when not o.has_res and not o.has_mod
              then (coalesce(t.deadline::date, t.publication_date) + interval '12 months')::date < v_cutoff end,
         m.legal_basis, m.procedure_type, m.buyer_name, m.buyer_identifier, m.buyer_country, m.buyer_legal_type,
         m.main_activity, m.title_en, m.title_original, m.description,
         coalesce(cp.cpv_codes, '{}'),
         coalesce(t.publication_date, o.first_result_published, m.publication_date),
         t.deadline,
         coalesce(o.decision_date, o.conclusion_date, o.result_published),
         case when o.decision_date is not null then 'decision_date'
              when o.conclusion_date is not null then 'contract_conclusion'
              when o.result_published is not null then 'result_publication' end,
         case when t.publication_date is not null
                   and coalesce(o.decision_date, o.conclusion_date, o.result_published) >= t.publication_date
              then coalesce(o.decision_date, o.conclusion_date, o.result_published) - t.publication_date end,
         t.estimated_value, t.estimated_currency,
         o.awarded_value,
         coalesce(o.currency, t.estimated_currency),
         o.n_lots,
         case when coalesce(o.n_lots, 1) <= 1 then o.quantity end,
         case when coalesce(o.n_lots, 1) <= 1 then o.quantity_unit end,
         case when coalesce(o.n_lots, 1) <= 1 and o.quantity > 0 and o.awarded_value > 0 and o.n_results = 1
              then round(o.awarded_value / o.quantity, 4) end,
         o.bids_received,
         coalesce(w.winners, '[]'),
         coalesce(o.consortium, false),
         o.reasons
  from o
  join n m on m.publication_number = o.main_pub
  left join n t on t.publication_number = o.tender
  left join cpvs cp on cp.gkey = o.gkey
  left join win w on w.gkey = o.gkey;
  get diagnostics v_rows = row_count;

  return jsonb_build_object(
    'procedures', v_rows,
    'by_outcome', (select jsonb_object_agg(s.outcome, s.c) from (select p.outcome, count(*) c from public.kb_baseline_procedures p
                    where p.snapshot_id = p_snapshot group by p.outcome) s),
    'by_link_method', (select jsonb_object_agg(coalesce(s.link_method, 'none'), s.c) from (select p.link_method, count(*) c
                    from public.kb_baseline_procedures p where p.snapshot_id = p_snapshot group by p.link_method) s));
end
$$;
revoke all on function public.kb_baseline_link(text) from public, anon, authenticated;
grant execute on function public.kb_baseline_link(text) to service_role;
