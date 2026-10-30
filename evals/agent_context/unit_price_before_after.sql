-- Unit-price priors before and after the 2026-10-30 fix (15:21-15:33 UTC). Read-only.
-- "before": the selection rule of the pre-fix agent_ctx_priors, replayed over the frozen kb_baseline_stats
--           (it took unit_price_p25/p50/p75 from the first stats row: family+calibre+country, then family+country,
--           family, class; rows with n_procedures >= 3 first).
-- "after":  agent_ctx_priors as deployed now (unit prices only from kb_baseline_unit_prices, same calibre only).
-- Scopes: every open tender (scope as agent_ctx_tender derives it) plus the three quote scopes of questions.json.
-- The EUR band per family is the same plausibility assumption as in eval.sql (not data).
-- psql "$DATABASE_URL" -qAt -f evals/agent_context/unit_price_before_after.sql
with fx(cur, eur) as (values ('EUR', 1.0), ('PLN', 0.23), ('CZK', 0.04), ('HUF', 0.0025), ('RON', 0.20), ('BGN', 0.511),
                             ('DKK', 0.134), ('SEK', 0.087), ('NOK', 0.085), ('GBP', 1.17), ('USD', 0.92), ('CHF', 1.05)),
band(family_id, lo_eur, hi_eur) as (values
  ('ammunition.small_calibre', 0.05, 25), ('ammunition.training_blank', 0.02, 25), ('ammunition.shotgun', 0.05, 25),
  ('ammunition.less_lethal', 0.1, 300), ('ammunition.medium_calibre', 1, 3000), ('ammunition.grenade_40mm', 5, 3000),
  ('ammunition.mortar', 50, 15000), ('ammunition.artillery', 100, 30000), ('firearms.handguns', 100, 6000),
  ('firearms.rifles_carbines', 200, 25000), ('firearms.submachine_guns', 300, 12000),
  ('firearms.machine_guns', 1000, 100000), ('misc_weapons.bladed', 2, 3000)),
snap as (select s.id from public.kb_snapshots s where s.active order by s.id limit 1),
scopes as (
  select distinct sc ->> 'family_id' as fam, sc ->> 'class_id' as cls, sc ->> 'calibre' as cal, sc ->> 'country' as ctry,
         string_agg(o.publication_number, ' ') over (partition by sc ->> 'family_id', sc ->> 'class_id', sc ->> 'calibre', sc ->> 'country') as subjects
  from public.ted_open_tenders o
  cross join lateral (select public.agent_ctx_tender(o.publication_number, 8) -> 'scope' as sc) t
  union
  select * from (values ('ammunition.small_calibre', 'ammunition', '9x19', 'POL', 'Q08/Q15 quote'),
                        ('ammunition.artillery', 'ammunition', '155mm', 'POL', 'Q16 quote'),
                        ('firearms.handguns', 'firearms', '9x19', 'POL', 'Q17 quote')) v
),
cmp as (
  select s.*,
         b.scope_level as before_scope, b.unit_price_p25 as before_p25, b.unit_price_p50 as before_p50,
         b.unit_price_p75 as before_p75, b.currency as before_cur, b.n_unit_prices as before_n,
         a.pr -> 'baseline' ->> 'unit_price_scope_level' as after_scope,
         public.agent_ctx_num(a.pr -> 'baseline' -> 'unit_price_p50') as after_p50,
         public.agent_ctx_num(a.pr -> 'baseline' -> 'unit_price_p25') as after_p25,
         public.agent_ctx_num(a.pr -> 'baseline' -> 'unit_price_p75') as after_p75,
         a.pr -> 'baseline' ->> 'currency' as after_cur, a.pr -> 'baseline' ->> 'n_unit_prices' as after_n
  from scopes s
  left join lateral (
    select b.* from public.kb_baseline_stats b, snap
    where b.snapshot_id = snap.id and coalesce(b.n_procedures, 0) > 0
      and ((s.fam is not null and b.scope_level = 'family_calibre_country' and b.family_id = s.fam and b.calibre = s.cal and b.country = s.ctry)
        or (s.fam is not null and b.scope_level = 'family_country' and b.family_id = s.fam and b.country = s.ctry)
        or (s.fam is not null and b.scope_level = 'family' and b.family_id = s.fam)
        or (s.cls is not null and b.scope_level = 'class' and b.class_id = s.cls))
    order by (b.n_procedures >= 3) desc,
             case b.scope_level when 'family_calibre_country' then 0 when 'family_country' then 1 when 'family' then 2 else 3 end,
             (b.buyer_legal_type is null) desc, b.n_procedures desc
    limit 1) b on true
  cross join lateral (select public.agent_ctx_priors(s.fam, s.cls, s.cal, s.ctry, 8) -> 'priors' as pr) a
),
judged as (
  select c.*,
         (select c.before_p50 * fx.eur from fx where fx.cur = upper(c.before_cur)) as before_eur,
         (select c.after_p50 * fx.eur from fx where fx.cur = upper(c.after_cur)) as after_eur,
         bd.lo_eur, bd.hi_eur
  from cmp c left join band bd on bd.family_id = c.fam
),
flag as (
  select j.*,
         j.before_p50 is not null and (j.before_eur is null or j.lo_eur is null or j.before_eur not between j.lo_eur and j.hi_eur
                                       or j.before_p75 > 20 * nullif(j.before_p25, 0)) as before_bad,
         j.after_p50 is not null and (j.after_eur is null or j.lo_eur is null or j.after_eur not between j.lo_eur and j.hi_eur
                                      or j.after_p75 > 20 * nullif(j.after_p25, 0)) as after_bad
  from judged j
),
lines as (
  select 0 as s, n::int as o, ''::text as k, line from unnest(array[
    '## Unit-price priors before vs after the fix',
    '',
    format('Run at %s UTC, active snapshot %s. "before" replays the pre-fix agent_ctx_priors rule over kb_baseline_stats; "after" calls agent_ctx_priors now. "suspect" = outside the assumed EUR band for the family, no band for the family, or p75 > 20 x p25.',
           to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI'), (select id from snap)),
    '',
    '| | scopes | with a unit price | suspect |',
    '|---|---|---|---|']) with ordinality u(line, n)
  union all
  select 1, 1, '', format('| before | %s | %s | %s |', count(*), count(*) filter (where before_p50 is not null), count(*) filter (where before_bad)) from flag
  union all
  select 1, 2, '', format('| after | %s | %s | %s |', count(*), count(*) filter (where after_p50 is not null), count(*) filter (where after_bad)) from flag
  union all
  select 2, n::int, '', line from unnest(array['', '| scope (family / calibre / country) | subjects | before: p50 (p25-p75) currency, scope, n | after: p50 (p25-p75) currency, scope, n |',
                                              '|---|---|---|---|']) with ordinality u(line, n)
  union all
  select 3, 0, coalesce(f.fam, f.cls, '') || coalesce(f.cal, '') || coalesce(f.ctry, ''),
         format('| %s / %s / %s | %s | %s | %s |', coalesce(f.fam, 'class ' || f.cls, '?'), coalesce(f.cal, '-'), coalesce(f.ctry, '-'),
                f.subjects,
                case when f.before_p50 is null then 'none'
                     else format('%s (%s-%s) %s, %s, n=%s%s', round(f.before_p50, 4), round(f.before_p25, 4), round(f.before_p75, 4),
                                 f.before_cur, f.before_scope, f.before_n, case when f.before_bad then ' **suspect**' else '' end) end,
                case when f.after_p50 is null then 'none'
                     else format('%s (%s-%s) %s, %s, n=%s%s', round(f.after_p50, 4), round(f.after_p25, 4), round(f.after_p75, 4),
                                 f.after_cur, f.after_scope, f.after_n, case when f.after_bad then ' **suspect**' else '' end) end)
  from flag f
  where f.before_p50 is not null or f.after_p50 is not null
)
select line from lines order by s, o, k;
