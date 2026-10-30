-- WS8 8.6: priors per scope (kb_baseline_stats) and historical patterns P001-P005 (kb_patterns).
-- Rebuildable while the snapshot is 'building'.
--
-- Scope levels: class; family; family + buyer country; family + calibre + buyer country. Narrow scopes are
-- written only with >= 3 procedures, so agent_context falls back to the next wider scope (§3.4 priors).
-- Money: one currency per row (the scope's dominant awarded currency); values in other currencies are left
-- out, never converted. Unit price only where the procedure states a single-lot quantity (BT-25) and has one
-- result notice; never a notice total divided by a guessed quantity. Winners exist only for eForms results
-- (Nov 2023 on), so winner shares describe that period.
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
  select * from public.kb_baseline_procedures pr where pr.snapshot_id = p_snapshot and pr.class_id is not null;

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
           count(*) filter (where k.unit_price is not null and k.currency = c.currency and k.quantity_unit is not distinct from qu.unit) as n_up,
           percentile_cont(0.25) within group (order by k.unit_price) filter (where k.unit_price is not null and k.currency = c.currency and k.quantity_unit is not distinct from qu.unit) as up25,
           percentile_cont(0.5) within group (order by k.unit_price) filter (where k.unit_price is not null and k.currency = c.currency and k.quantity_unit is not distinct from qu.unit) as up50,
           percentile_cont(0.75) within group (order by k.unit_price) filter (where k.unit_price is not null and k.currency = c.currency and k.quantity_unit is not distinct from qu.unit) as up75,
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
         b.n_up,
         round(b.up25::numeric, 4), round(b.up50::numeric, 4), round(b.up75::numeric, 4),
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
  where b.lvl in ('class', 'family') or b.n >= 3;
  get diagnostics v_stats = row_count;

  -- ---------------------------------------------------------------- patterns
  -- P001 demand: buyers re-buying the same family, with the interval
  insert into public.kb_patterns (snapshot_id, id, kind, scope, statement, support, examples, metrics)
  with p as (
    select x.*, 'ted:' || coalesce(x.tender_publication_number, x.result_publication_numbers[1], x.modification_publication_numbers[1]) as ref,
           lower(regexp_replace(coalesce(x.buyer_name, ''), '[^[:alnum:]]+', ' ', 'g')) || '|' || coalesce(x.buyer_country, '') as buyer_key
    from pg_temp.kb_bs_p x
    where x.family_id is not null and x.family_id <> 'out_of_scope.noise' and x.published is not null
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
  select p_snapshot, 'P001', 'demand', '{}'::jsonb,
         format('%s of %s buyer-family pairs bought the same product family at least twice between %s and %s; the median interval between two purchases is %s days. Most re-bought: %s.',
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

  -- P002 supplier: winner concentration per family and buyer country (awards with named winners)
  insert into public.kb_patterns (snapshot_id, id, kind, scope, statement, support, examples, metrics)
  with w as (
    select distinct x.procedure_id, x.family_id, x.buyer_country, lower(e ->> 'name') as wkey, e ->> 'name' as name,
           e ->> 'country' as wcountry,
           'ted:' || coalesce(x.tender_publication_number, x.result_publication_numbers[1]) as ref, x.published
    from pg_temp.kb_bs_p x, jsonb_array_elements(x.winners) e
    where x.result_status = 'awarded' and x.family_id is not null and x.family_id <> 'out_of_scope.noise'
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
  select p_snapshot, 'P002', 'supplier', '{}'::jsonb,
         format('In %s family-country markets with at least 5 awards to named winners (eForms results, Nov 2023 on), the largest winner takes a median %s%% of the awards. Most concentrated: %s.',
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
                            'method', 'consortium = a result notice with more winners than tendering parties (eForms tendering-party-name)')
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
                            'basis', 'awarded_value = sum of result notice totals in the dominant currency; multi-lot notices are not split')
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
    'patterns', v_patterns,
    'patterns_by_kind', (select jsonb_object_agg(z.kind, z.c) from (select t.kind, count(*) c from public.kb_patterns t
                          where t.snapshot_id = p_snapshot group by t.kind) z));
end
$$;
revoke all on function public.kb_baseline_build_stats(text) from public, anon, authenticated;
grant execute on function public.kb_baseline_build_stats(text) to service_role;
