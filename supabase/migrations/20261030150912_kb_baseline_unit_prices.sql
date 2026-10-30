-- Fix-forward for unit prices of frozen snapshots (read-only; baseline_v1 rows are not changed).
-- kb_baseline_stats.unit_price_* of baseline_v1 also used procedures whose stated quantity is 1 (a lump sum:
-- the "unit price" equals the notice total) or has no unit code (BT-625 missing). 23 stats rows carry unit
-- prices; agent_context should read unit-price priors from this view instead. Rule: single lot, one result
-- notice, quantity > 1 in pieces (H87), value in the procedure's currency. Same four scope levels as the stats.
create or replace view public.kb_baseline_unit_prices with (security_invoker = on) as
with p as (
  select x.snapshot_id, x.class_id, x.family_id, x.calibre, x.buyer_country as country, x.currency, x.unit_price,
         x.quantity, x.published,
         'ted:' || coalesce(x.tender_publication_number, x.result_publication_numbers[1]) as ref
  from public.kb_baseline_procedures x
  where x.unit_price is not null and x.quantity > 1 and x.quantity_unit = 'H87' and x.currency is not null
    and x.class_id is not null
),
s as (
  select 'class' as scope_level, p.class_id, null::text as family_id, null::text as calibre, null::text as country,
         p.snapshot_id, p.currency, p.unit_price, p.quantity, p.published, p.ref
  from p
  union all
  select 'family', p.class_id, p.family_id, null, null, p.snapshot_id, p.currency, p.unit_price, p.quantity, p.published, p.ref
  from p where p.family_id is not null
  union all
  select 'family_country', p.class_id, p.family_id, null, p.country, p.snapshot_id, p.currency, p.unit_price, p.quantity, p.published, p.ref
  from p where p.family_id is not null and p.country is not null
  union all
  select 'family_calibre_country', p.class_id, p.family_id, p.calibre, p.country, p.snapshot_id, p.currency, p.unit_price,
         p.quantity, p.published, p.ref
  from p where p.family_id is not null and p.calibre is not null and p.country is not null
)
select s.snapshot_id, s.scope_level, s.class_id, s.family_id, s.calibre, s.country, s.currency,
       'H87'::text as quantity_unit,
       count(*)::int as n,
       round(percentile_cont(0.25) within group (order by s.unit_price)::numeric, 4) as unit_price_p25,
       round(percentile_cont(0.5) within group (order by s.unit_price)::numeric, 4) as unit_price_p50,
       round(percentile_cont(0.75) within group (order by s.unit_price)::numeric, 4) as unit_price_p75,
       percentile_cont(0.5) within group (order by s.quantity) as quantity_p50,
       (array_agg(s.ref order by s.published desc))[1:5] as example_refs,
       'awarded notice total / stated single-lot quantity (pieces, quantity > 1, one result notice)'::text as basis
from s
group by s.snapshot_id, s.scope_level, s.class_id, s.family_id, s.calibre, s.country, s.currency;

grant select on public.kb_baseline_unit_prices to anon, authenticated, service_role;
