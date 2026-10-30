-- Semantic retrieval quality without an API key. Read-only.
-- Query vector = the stored Gemini embedding of an open tender's own kb_chunks card (no Gemini call).
-- For each open tender: the 5 nearest past cards (kind tender / award / contract, other notices, no open tender),
-- once over all layers and once without layer = baseline. A hit is relevant when it has
--   family:   the same product family label (kb_chunks.family_id; null = miss),
--   calibre:  the same calibre label, or the calibre written in its title/content (regex below),
--   class:    the same top-level class (ammunition / firearms / parts / misc_weapons).
-- "cross-language" = the hit's original notice language differs from the query notice's language.
-- Exact nearest neighbours: "+ 0" in the ORDER BY keeps the planner off the approximate HNSW index.
-- psql "$DATABASE_URL" -qAt -f evals/agent_context/retrieval_p5.sql
with q(pub, family_id, calibre, cal_re) as (values
  ('637078-2026', 'ammunition.small_calibre',  '7.62x51', '7[,.]62 ?(x|×) ?51|7[,.]62 ?mm nato'),
  ('672444-2026', 'ammunition.mortar',         '81mm',    '81 ?mm'),
  ('659995-2026', 'firearms.launchers',        '40mm',    '40 ?mm'),
  ('671826-2026', 'ammunition.training_blank', '5.56x45', '5[,.]56'),
  ('615123-2026', 'ammunition.small_calibre',  '5.56x45', '5[,.]56'),
  ('646341-2026', 'ammunition.small_calibre',  '4.6x30',  '4[,.]6 ?(x|×) ?30'),
  ('695606-2026', 'ammunition.artillery',      '155mm',   '155 ?mm'),
  ('397552-2026', 'ammunition.small_calibre',  '9x19',    '9 ?(x|×) ?19|9 ?mm|parabellum'),
  ('668765-2026', 'ammunition.small_calibre',  '12.7x99', '12[,.]7'),
  ('730745-2026', 'ammunition.small_calibre',  '7.62x39', '7[,.]62 ?(x|×) ?39'),
  ('749708-2026', 'ammunition.heavy_ordnance', '130mm',   '130 ?mm'),
  ('630124-2026', 'firearms.submachine_guns',  '9x19',    '9 ?(x|×) ?19|9 ?mm'),
  ('653973-2026', 'ammunition.small_calibre',  '7.62x39', '7[,.]62 ?(x|×) ?39'),
  ('692607-2026', 'firearms.handguns',         '9x19',    '9 ?(x|×) ?19|9 ?mm')
),
qv as (
  select q.*, k.embedding as emb, coalesce(n.original_language, '?') as qlang
  from q
  cross join lateral (select c.embedding from public.kb_chunks c
                      where c.kind = 'tender' and c.publication_number = q.pub and c.embedding is not null
                      order by (c.layer = 'ted_live') desc, c.updated_at desc limit 1) k
  left join public.ted_notices n on n.publication_number = q.pub
),
hits as (
  select qv.pub, v.variant, h.*
  from qv cross join (values ('all layers'), ('without baseline')) v(variant)
  cross join lateral (
    select k.ref, k.kind, k.layer, k.family_id as hfam, k.calibre as hcal, k.publication_number as hpub,
           concat_ws(' ', k.title, k.content) as htxt,
           row_number() over (order by (k.embedding operator(extensions.<=>) qv.emb) + 0, k.id) as rn
    from public.kb_chunks k
    where k.embedding is not null and k.kind in ('tender', 'award', 'contract')
      and k.publication_number is distinct from qv.pub
      and not exists (select 1 from public.ted_open_tenders o where o.publication_number = k.publication_number)
      and (v.variant = 'all layers' or k.layer <> 'baseline')
    order by (k.embedding operator(extensions.<=>) qv.emb) + 0, k.id
    limit 5) h
),
judged as (
  select h.*, q.family_id, q.calibre, q.qlang,
         coalesce(h.hfam = q.family_id, false) as fam_ok,
         coalesce(split_part(h.hfam, '.', 1) = split_part(q.family_id, '.', 1), false) as class_ok,
         coalesce(h.hcal = q.calibre, false) or h.htxt ~* q.cal_re as cal_ok,
         coalesce(bn.original_language, tn.original_language) as hlang
  from hits h join qv q on q.pub = h.pub
  left join public.kb_baseline_notices bn on bn.snapshot_id = 'baseline_v1' and bn.publication_number = h.hpub
  left join public.ted_notices tn on tn.publication_number = h.hpub
),
per as (
  select j.pub, j.variant, min(j.family_id) as family_id, min(j.calibre) as calibre, min(j.qlang) as qlang, count(*) as n,
         avg(j.fam_ok::int) as p_fam, avg(j.class_ok::int) as p_class, avg(j.cal_ok::int) as p_cal,
         avg((j.fam_ok and j.cal_ok)::int) as p_fam_cal,
         count(*) filter (where j.hlang is not null and j.hlang <> j.qlang) as xlang,
         count(*) filter (where j.hlang is not null and j.hlang <> j.qlang and j.fam_ok) as xlang_fam_ok,
         count(*) filter (where j.layer = 'baseline') as from_baseline,
         count(*) - count(distinct j.hpub) as same_notice_twice,
         string_agg(j.hpub || '(' || coalesce(j.hlang, '?') || ',' || coalesce(j.hfam, 'no family') || ','
                    || case when j.cal_ok then 'cal ok' else 'cal no' end || ')', ' ' order by j.rn) as top5
  from judged j group by j.pub, j.variant
),
lines as (
  select 0 as s, n::int as o, ''::text as k, line from unnest(array[
    '## Semantic retrieval precision@5 (stored tender embedding as the query, no API key)',
    '',
    format('Run at %s UTC, active snapshot %s.', to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI'),
           (select string_agg(s.id, ', ') from public.kb_snapshots s where s.active)),
    '',
    '| layers | tenders | P@5 same family | P@5 same class | P@5 same calibre | P@5 family + calibre | cross-language hits (of which same family) | hits from baseline | same notice twice in top 5 |',
    '|---|---|---|---|---|---|---|---|---|']) with ordinality u(line, n)
  union all
  select 1, case p.variant when 'all layers' then 1 else 2 end, '',
         format('| %s | %s | %s | %s | %s | %s | %s/%s (%s) | %s | %s |', p.variant, count(*),
                round(avg(p.p_fam), 2), round(avg(p.p_class), 2), round(avg(p.p_cal), 2), round(avg(p.p_fam_cal), 2),
                sum(p.xlang), sum(p.n), sum(p.xlang_fam_ok), sum(p.from_baseline), sum(p.same_notice_twice))
  from per p group by p.variant
  union all
  select 2, n::int, '', line from unnest(array['', '| tender | layers | family / calibre / lang | P@5 family | P@5 calibre | top 5 (pub(lang, family, calibre)) |',
                                          '|---|---|---|---|---|---|']) with ordinality u(line, n)
  union all
  select 3, case p.variant when 'all layers' then 1 else 2 end, p.pub,
         format('| %s | %s | %s / %s / %s | %s | %s | %s |', p.pub, p.variant, p.family_id, p.calibre, p.qlang,
                round(p.p_fam, 1), round(p.p_cal, 1), p.top5)
  from per p
)
select line from lines order by s, k, o;
