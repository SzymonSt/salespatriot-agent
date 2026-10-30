#!/usr/bin/env python3
"""Build the agent_context evaluation SQL from questions.json.

    python3 evals/agent_context/build_eval.py            # writes ground_truth.sql and eval.sql
    python3 evals/agent_context/build_eval.py --check    # also fails if questions.json SQL is stale
    python3 evals/agent_context/build_eval.py --write-questions   # re-render ground_truth_sql in questions.json

Outputs (next to this file, all read-only SQL):
  ground_truth.sql  one row per question: the expected answer computed from the source tables
                    (kb_baseline_procedures, ted_notices, ted_awards, supplier_contracts), never via agent_context
  eval.sql          calls agent_context per question, emulates the same pack without the baseline layer,
                    scores recall of the expected items, wrong numbers, warnings, size and latency;
                    prints markdown lines (psql -qAt -f eval.sql)
Standard library only.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

def lit(v):
    if v is None:
        return "null"
    return "'" + str(v).replace("'", "''") + "'"


PROC_TEMPLATE = """with prm(id, country, cpv_re, txt_re, not_re, buyer_re, buyer_ident) as (values
{rows}
),
p as (
  select prm.id, coalesce(b.result_publication_numbers[1], b.tender_publication_number) as ref,
         array_remove(array[b.tender_publication_number] || b.result_publication_numbers, null) as pubs,
         b.outcome, b.published, b.bids_received, b.currency, b.quantity, b.quantity_unit, b.unit_price, b.winners
  from prm join public.kb_baseline_procedures b on b.snapshot_id = 'baseline_v1'
   and (prm.country is null or b.buyer_country = prm.country)
   and (prm.cpv_re is null or exists (select 1 from unnest(b.cpv_codes) c where c ~ prm.cpv_re))
   and (prm.txt_re is null or concat_ws(' ', b.title_en, b.title_original, b.description) ~* prm.txt_re)
   and (prm.not_re is null or concat_ws(' ', b.title_en, b.title_original, b.description) !~* prm.not_re)
   and ((prm.buyer_re is null and prm.buyer_ident is null)
        or b.buyer_name ~* prm.buyer_re or b.buyer_identifier = prm.buyer_ident)
),
l as (
  select prm.id, a.publication_number as ref, a.winner_name as name
  from prm join public.ted_notices n
    on (prm.country is null or n.buyer_country = prm.country)
   and (prm.cpv_re is null or exists (select 1 from unnest(n.cpv_codes) c where c ~ prm.cpv_re))
   and (prm.txt_re is null or concat_ws(' ', n.title_en, n.title_original, n.description) ~* prm.txt_re)
   and (prm.not_re is null or concat_ws(' ', n.title_en, n.title_original, n.description) !~* prm.not_re)
   and ((prm.buyer_re is null and prm.buyer_ident is null)
        or n.buyer_name ~* prm.buyer_re or n.buyer_identifier = prm.buyer_ident)
  join public.ted_awards a on a.publication_number = n.publication_number
),
w as (
  select p.id, lower(btrim(regexp_replace(x ->> 'name', '[„”“"]', '', 'g'))) as name, p.ref
  from p cross join lateral jsonb_array_elements(coalesce(p.winners, '[]'::jsonb)) x
  union
  select l.id, lower(btrim(regexp_replace(l.name, '[„”“"]', '', 'g'))), l.ref from l
)
select prm.id, jsonb_build_object(
  'n_procedures', (select count(*) from p where p.id = prm.id),
  'n_awarded', (select count(*) from p where p.id = prm.id and p.outcome = 'awarded'),
  'bids_p50_known', (select percentile_cont(0.5) within group (order by p.bids_received) from p
                     where p.id = prm.id and p.bids_received > 0),
  'unit_prices', (select jsonb_agg(jsonb_build_object('pubs', p.pubs, 'unit_price', p.unit_price, 'currency', p.currency,
                                                      'quantity', p.quantity, 'unit', p.quantity_unit))
                  from p where p.id = prm.id and p.unit_price is not null),
  'top_winners', (select jsonb_agg(jsonb_build_object('name', t.name, 'notices', t.n) order by t.n desc, t.name)
                  from (select w.name, count(distinct w.ref) as n from w where w.id = prm.id and w.name <> ''
                        group by w.name order by 2 desc, 1 limit 8) t),
  'recent_awarded', (select jsonb_agg(t.pubs order by t.published desc)
                     from (select p.pubs, p.published from p where p.id = prm.id and p.outcome = 'awarded'
                           order by p.published desc limit 5) t)
) as ground_truth
from prm"""

SUPPLIER_TEMPLATE = """with prm(id, supplier_id, name_re) as (values
{rows}
),
w as (   -- award notices won by the supplier, from every source (publication date of the award notice)
  select prm.id, sc.notice_number as pub, sc.published as d, 'supplier_contracts' as src
  from prm join public.supplier_contracts sc on sc.supplier_id = prm.supplier_id
  union
  select prm.id, a.publication_number, n.publication_date, 'ted_awards'
  from prm join public.ted_awards a on a.winner_name ~* prm.name_re
  join public.ted_notices n on n.publication_number = a.publication_number
  union   -- winners per result notice (not per procedure: a procedure's later result notices can name other winners)
  select prm.id, bn.publication_number, bn.publication_date, 'kb_baseline_notices'
  from prm join public.kb_baseline_notices bn on bn.snapshot_id = 'baseline_v1' and bn.kind = 'result'
  cross join lateral jsonb_array_elements(coalesce(bn.winners, '[]'::jsonb)) x
  where x ->> 'name' ~* prm.name_re
),
d as (select w.id, w.pub, max(w.d) as d, array_agg(distinct w.src order by w.src) as srcs from w group by 1, 2)
select prm.id, jsonb_build_object(
  'award_notices', (select count(*) from d where d.id = prm.id),
  'only_in_baseline', (select count(*) from d where d.id = prm.id and d.srcs = array['kb_baseline_notices']),
  'recent_award_notices', (select jsonb_agg(jsonb_build_object('pub', t.pub, 'date', t.d, 'sources', t.srcs) order by t.d desc, t.pub)
                           from (select * from d where d.id = prm.id order by d.d desc nulls last, d.pub limit 8) t)
) as ground_truth
from prm"""

SEARCH_TEMPLATE = """with prm(id, country, cpv_re, txt_re) as (values
{rows}
),
p as (
  select prm.id, array_remove(array[b.tender_publication_number] || b.result_publication_numbers, null) as pubs, b.published
  from prm join public.kb_baseline_procedures b on b.snapshot_id = 'baseline_v1'
   and (prm.country is null or b.buyer_country = prm.country)
   and (prm.cpv_re is null or exists (select 1 from unnest(b.cpv_codes) c where c ~ prm.cpv_re))
   and concat_ws(' ', b.title_en, b.title_original, b.description) ~* prm.txt_re
),
l as (
  select prm.id, n.publication_number, n.publication_date
  from prm join public.ted_notices n
    on (prm.country is null or n.buyer_country = prm.country)
   and (prm.cpv_re is null or exists (select 1 from unnest(n.cpv_codes) c where c ~ prm.cpv_re))
   and concat_ws(' ', n.title_en, n.title_original, n.description) ~* prm.txt_re
)
select prm.id, jsonb_build_object(
  'baseline_procedures', (select count(*) from p where p.id = prm.id),
  'procedures', (select jsonb_agg(p.pubs order by p.published desc) from p where p.id = prm.id),
  'live_notices', (select jsonb_agg(l.publication_number order by l.publication_date desc) from l where l.id = prm.id)
) as ground_truth
from prm"""


TEMPLATES = {
    "procedures": (PROC_TEMPLATE, ["country", "cpv_re", "txt_re", "not_re", "buyer_re", "buyer_ident"]),
    "supplier": (SUPPLIER_TEMPLATE, ["supplier_id", "name_re"]),
    "search": (SEARCH_TEMPLATE, ["country", "cpv_re", "txt_re"]),
}


def render(qid, gt):
    tpl, cols = TEMPLATES[gt["template"]]
    row = "  (" + ", ".join([lit(qid)] + [lit(gt["params"][c]) + ("::text" if i == 0 else "") for i, c in enumerate(cols)]) + ")"
    return tpl.format(rows=row)



def ground_truth_sql(questions):
    """One statement, one row per question: (id, ground_truth jsonb)."""
    parts = []
    for name, (tpl, cols) in TEMPLATES.items():
        rows = [q for q in questions if q["ground_truth"]["template"] == name]
        if not rows:
            continue
        values = ",\n".join(
            "  (" + ", ".join([lit(q["id"])] + [lit(q["ground_truth"]["params"][c]) + ("::text" if i == 0 else "")
                                                 for i, c in enumerate(cols)]) + ")"
            for q in rows)
        parts.append("select * from (\n" + tpl.format(rows=values) + "\n) " + name)
    return ("-- Generated by build_eval.py from questions.json. Read-only.\n"
            "-- Ground truth per question, computed from the source tables (not from agent_context).\n"
            + "\nunion all\n".join(parts) + "\norder by 1;\n")


# Columns of kb_chunks filters shared by the three branches of the kb_search replica.
KB_FILTERS = """(pk.e_kinds is null or k.kind = any (pk.e_kinds))
            and (pk.e_fam is null or k.family_id = pk.e_fam or k.class_id = pk.e_fam)
            and (pk.e_cal is null or k.calibre is null or k.calibre = pk.e_cal)
            and (pk.e_cpv is null or exists (select 1 from unnest(k.cpv_codes) cc where cc like pk.e_cpv || '%'))
            and (pk.e_country is null or k.country = pk.e_country)
            and (k.visibility = 'public' or (pk.viewer is not null and pk.viewer = any (k.party_ids)))
            and (not v.nobase or k.layer <> 'baseline')"""

# Exact copy of public.kb_search + agent_ctx_evidence (as deployed on 2026-10-30) with one extra switch:
# v.nobase = true drops layer 'baseline'. With v.nobase = false it must reproduce the pack's evidence.
KB_SEARCH_REPLICA = """(with recursive
  lx as (
    select s.l, row_number() over () as rn
    from (select l from unnest(tsvector_to_array(to_tsvector('simple', coalesce(pk.qtext, '')))) l
          where length(l) > 1 limit 24) s
  ),
  fold as (
    select 1 as rn, plainto_tsquery('simple', lx.l) as q from lx where lx.rn = 1
    union all
    select f.rn + 1, f.q || plainto_tsquery('simple', lx.l) from fold f join lx on lx.rn = f.rn + 1
  ),
  tq as (
    select a.v_and,
           case when pk.qtext is null then null
                else coalesce((select f.q from fold f order by f.rn desc limit 1), a.v_and) end as v_or
    from (select case when pk.qtext is not null and numnode(websearch_to_tsquery('simple', pk.qtext)) > 0
                      then websearch_to_tsquery('simple', pk.qtext) end as v_and) a
  ),
  vec as (
    select c.id, row_number() over (order by c.dist, c.id) as r
    from (select k.id, k.embedding operator(extensions.<=>) pk.emb as dist
          from public.kb_chunks k
          where pk.emb is not null and k.embedding is not null
            and @@FILTERS@@
          order by k.embedding operator(extensions.<=>) pk.emb
          limit 40) c
  ),
  txt as (
    select c.id, row_number() over (order by c.and_hit desc, c.rank desc, c.id) as r
    from (select k.id, coalesce(k.fts @@ tq.v_and, false) as and_hit, ts_rank_cd(k.fts, tq.v_or) as rank
          from public.kb_chunks k cross join tq
          where tq.v_or is not null and k.fts @@ tq.v_or
            and @@FILTERS@@
          order by 2 desc, 3 desc, k.id
          limit 40) c
  ),
  hits as (select coalesce(v2.id, t.id) as id, v2.r as vr, t.r as tr from vec v2 full join txt t on t.id = v2.id),
  ranked as (
    select k.ref, k.kind, k.layer, k.title, k.content, k.facts, k.occurred_at,
           coalesce(1.0 / (60 + h.vr), 0) + coalesce(1.0 / (60 + h.tr), 0)
             + case when pk.e_cal is not null and k.calibre = pk.e_cal then 0.01 else 0 end as score
    from hits h join public.kb_chunks k on k.id = h.id
    union all
    select f.ref, f.kind, f.layer, f.title, f.content, f.facts, f.occurred_at,
           case when pk.e_cal is not null and f.calibre = pk.e_cal then 0.01 else 0 end
    from (select k.* from public.kb_chunks k cross join tq
          where pk.emb is null and tq.v_or is null
            and @@FILTERS@@
          order by coalesce(k.calibre = pk.e_cal, false) desc, k.occurred_at desc nulls last, k.ref
          limit pk.e_klimit) f
  ),
  top as (
    select r.*, row_number() over (order by r.score desc, r.occurred_at desc nulls last, r.ref) as o
    from (select * from ranked order by score desc, occurred_at desc nulls last, ref limit pk.e_klimit) r
  )
  select coalesce(jsonb_agg(jsonb_build_object('ref', t.ref, 'kind', t.kind, 'layer', t.layer, 'title', t.title,
                                               'snippet', left(t.content, 300), 'facts', t.facts,
                                               'score', round(t.score, 5)) order by t.o), '[]'::jsonb)
  from (select * from top where coalesce(top.ref, '') <> '' and not (top.ref = any (pk.e_excl))
        order by top.o limit pk.e_lim) t
)""".replace("@@FILTERS@@", KB_FILTERS)

# Exact copy of agent_ctx_company_chunks (check_counterparty evidence) with the same switch.
COMPANY_REPLICA = """(select coalesce(jsonb_agg(jsonb_build_object('ref', k.ref, 'kind', k.kind, 'layer', k.layer, 'title', k.title,
                                          'snippet', left(k.content, 300), 'facts', k.facts, 'score', null)
                       order by k.occurred_at desc nulls last, k.ref), '[]'::jsonb)
 from (select c.* from public.kb_chunks c
       where ((pk.c_sup is not null and c.supplier_id = pk.c_sup and c.kind in ('contract', 'award'))
           or (pk.c_buy is not null and c.buyer_id = pk.c_buy and c.kind in ('tender', 'award'))
           or (c.kind = 'conversation' and pk.viewer is not null and pk.viewer = any (c.party_ids)
               and (pk.c_sup = any (c.party_ids) or pk.c_buy = any (c.party_ids))))
         and (c.visibility = 'public' or (pk.viewer is not null and pk.viewer = any (c.party_ids)))
         and (not v.nobase or c.layer <> 'baseline')
       order by c.occurred_at desc nulls last, c.ref
       limit pk.e_lim) k)"""

# Same benchmark as agent_context step 7 (evaluate_quote, as deployed 2026-10-30 15:33 UTC), from a quote, price anchors
# and priors. Used for the emulated quotes (no conversation row: the eval is read-only) and for the no-history variant.
BENCHMARK = """(select case when b.basis is null then null else jsonb_build_object(
           'basis', b.basis, 'n', b.n, 'p25', round(b.p25, 4), 'p50', round(b.p50, 4), 'p75', round(b.p75, 4),
           'compared_value', b.cmp, 'currency', qv.cur,
           'position', case when b.cmp < b.p25 then 'below_p25' when b.cmp > b.p75 then 'above_p75'
                            else 'within_p25_p75' end) end
 from (select public.agent_ctx_num(@@Q@@ -> 'unit_price') as u, upper(nullif(btrim(@@Q@@ ->> 'currency'), '')) as cur,
              coalesce(public.agent_ctx_num(@@Q@@ -> 'total_price'), public.agent_ctx_num(@@Q@@ -> 'total'),
                       public.agent_ctx_num(@@Q@@ -> 'unit_price') * public.agent_ctx_num(@@Q@@ -> 'quantity')) as tot) qv
 cross join lateral (
   select count(*)::int as n, percentile_cont(0.25) within group (order by (x ->> 'unit_price')::numeric) as p25,
          percentile_cont(0.5) within group (order by (x ->> 'unit_price')::numeric) as p50,
          percentile_cont(0.75) within group (order by (x ->> 'unit_price')::numeric) as p75
   from jsonb_array_elements(coalesce(@@A@@, '[]'::jsonb)) x
   where x ->> 'unit_price' is not null and upper(x ->> 'currency') = qv.cur) ba
 cross join lateral (   -- c) since 15:33 UTC on 2026-10-30: framework ceilings, values < 100 and repeated totals left out
   select count(*)::int as n, percentile_cont(0.25) within group (order by s.v) as p25,
          percentile_cont(0.5) within group (order by s.v) as p50,
          percentile_cont(0.75) within group (order by s.v) as p75
   from (select distinct x ->> 'publication_number' as pub, (x ->> 'value')::numeric as v
         from jsonb_array_elements(coalesce(@@A@@, '[]'::jsonb)) x
         where x ->> 'kind' = 'contract' and x ->> 'value' is not null and upper(x ->> 'currency') = qv.cur
           and not coalesce((x ->> 'framework_agreement')::boolean, false) and (x ->> 'value')::numeric >= 100) s) bc
 cross join lateral (
   select * from (
     select 'anchor_unit_prices' as basis, ba.n, ba.p25::numeric as p25, ba.p50::numeric as p50, ba.p75::numeric as p75,
            qv.u as cmp, 1 as prio
     where qv.u is not null and qv.cur is not null and ba.n >= 3
     union all
     select 'baseline_unit_price', (@@P@@ -> 'baseline' ->> 'n_unit_prices')::int,
            (@@P@@ -> 'baseline' ->> 'unit_price_p25')::numeric,
            coalesce((@@P@@ -> 'blend' ->> 'unit_price_p50')::numeric, (@@P@@ -> 'baseline' ->> 'unit_price_p50')::numeric),
            (@@P@@ -> 'baseline' ->> 'unit_price_p75')::numeric, qv.u, 2
     where qv.u is not null and qv.cur is not null and @@P@@ -> 'baseline' ->> 'unit_price_p25' is not null
       and upper(@@P@@ -> 'baseline' ->> 'currency') = qv.cur
     union all
     select 'awarded_lot_values', bc.n, bc.p25::numeric, bc.p50::numeric, bc.p75::numeric, qv.tot, 3
     where qv.tot is not null and qv.cur is not null and bc.n >= 3
   ) z order by prio limit 1) b)"""


def benchmark(q, a, p):
    return BENCHMARK.replace("@@Q@@", q).replace("@@A@@", a).replace("@@P@@", p)


# Plausibility bands, EUR per unit, and rough FX to EUR. These are OUR assumptions (generous ranges to catch
# order-of-magnitude errors), not data. A number outside its band is reported as "wrong or misleading".
FX = [("EUR", 1.0), ("PLN", 0.23), ("CZK", 0.04), ("HUF", 0.0025), ("RON", 0.20), ("BGN", 0.511), ("DKK", 0.134),
      ("SEK", 0.087), ("NOK", 0.085), ("GBP", 1.17), ("USD", 0.92), ("CHF", 1.05)]
BANDS = [("ammunition.small_calibre", 0.05, 25), ("ammunition.training_blank", 0.02, 25), ("ammunition.shotgun", 0.05, 25),
         ("ammunition.less_lethal", 0.1, 300), ("ammunition.medium_calibre", 1, 3000), ("ammunition.grenade_40mm", 5, 3000),
         ("ammunition.mortar", 50, 15000), ("ammunition.artillery", 100, 30000), ("firearms.handguns", 100, 6000),
         ("firearms.rifles_carbines", 200, 25000), ("firearms.submachine_guns", 300, 12000),
         ("firearms.machine_guns", 1000, 100000), ("misc_weapons.bladed", 2, 3000)]


def eval_sql(questions, run_label):
    def slim(e):   # only what eval.sql reads; type defaults to text
        return {k: v for k, v in e.items() if k in ("label", "re", "path", "value", "tol") or (k == "type" and v != "text")}

    qjson = json.dumps([{"id": q["id"], "purpose": q["purpose"], "role": q["role"], "viewer": q.get("viewer"),
                         "subject": q["subject"], "query_text": q.get("query_text"),
                         "expected": [slim(e) for e in q["expected"]],
                         "proxy_embedding_pub": q.get("proxy_embedding_pub"), "quote": q.get("quote"),
                         "question": q["question"]} for q in questions],
                       ensure_ascii=False, separators=(",", ":"))
    assert "$qs$" not in qjson
    fx = ", ".join(f"('{c}', {r})" for c, r in FX)
    bands = ", ".join(f"('{f}', {lo}, {hi})" for f, lo, hi in BANDS)
    sql = f"""-- Generated by build_eval.py from questions.json ({run_label}). Read-only: SELECTs and agent_context only.
-- Output: markdown lines. psql "$DATABASE_URL" -qAt -f evals/agent_context/eval.sql > /tmp/eval.md
-- About 40 agent_context calls. Most take < 0.5 s, but the first calls of a session took up to 12 s on 2026-10-30.
set statement_timeout = '15min';
with q as (
  select * from jsonb_to_recordset($qs${qjson}$qs$::jsonb)
    as x(id text, purpose text, role text, viewer text, subject jsonb, query_text text, expected jsonb,
         proxy_embedding_pub text, quote jsonb, question text)
),
fx(cur, eur) as (values {fx}),
band(family_id, lo_eur, hi_eur) as (values {bands}),
modes as (
  select q.*, 'text'::text as mode, null::extensions.vector as emb from q
  union all
  select q.*, 'embed', (select k.embedding from public.kb_chunks k
                        where k.kind = 'tender' and k.publication_number = q.proxy_embedding_pub and k.embedding is not null
                        order by (k.layer = 'ted_live') desc, k.updated_at desc limit 1)
  from q where q.proxy_embedding_pub is not null
),
raw as (
  select m.*, case when m.purpose = 'evaluate_quote_emulated' then null
                   else public.agent_context(m.role, m.purpose, m.subject, m.emb, m.query_text, m.viewer, 8) end as pack0
  from modes m
),
qe as (   -- evaluate_quote without a conversation row (read-only run): same functions agent_context uses in step 6
  select r.id, r.mode, f.id as fam, f.parent_id as cls,
         coalesce(public.product_normalise_calibre(r.subject ->> 'calibre'), r.subject ->> 'calibre') as cal,
         upper(r.subject ->> 'country') as ctry
  from raw r left join public.product_families f on f.id = r.subject ->> 'family_id'
  where r.purpose = 'evaluate_quote_emulated'
),
qe2 as (
  select qe.*, public.agent_ctx_priors(qe.fam, qe.cls, qe.cal, qe.ctry, 8) as pr, public.agent_ctx_anchors(qe.fam, qe.cal, 8) as an
  from qe
),
packs as (
  select r.*, coalesce(r.pack0, jsonb_build_object(
           'purpose', 'evaluate_quote_emulated', 'role', r.role, 'subject', r.subject,
           'scope', jsonb_build_object('family_id', e.fam, 'class_id', e.cls, 'calibre', e.cal, 'country', e.ctry),
           'facts', jsonb_build_object('quote', r.quote || jsonb_build_object('benchmark',
                      {benchmark('r.quote', "e.an -> 'items'", "e.pr -> 'priors'")})),
           'candidates', '[]'::jsonb, 'evidence', '[]'::jsonb, 'price_anchors', e.an -> 'items', 'regulatory', '{{}}'::jsonb,
           'memory', '[]'::jsonb, 'priors', e.pr -> 'priors',
           'warnings', coalesce(e.pr -> 'warnings', '[]'::jsonb) || coalesce(e.an -> 'warnings', '[]'::jsonb),
           'versions', jsonb_build_object('elapsed_ms', null))) as pack
  from raw r left join qe2 e on e.id = r.id and e.mode = r.mode
),
pk as (   -- inputs of the evidence call, exactly as agent_context derives them
  select p.*, nullif(btrim(p.query_text), '') as qtext,
         nullif(btrim(coalesce(p.pack -> 'scope' ->> 'family_id', p.pack -> 'scope' ->> 'class_id')), '') as e_fam,
         coalesce(public.product_normalise_calibre(nullif(btrim(p.pack -> 'scope' ->> 'calibre'), '')),
                  nullif(btrim(p.pack -> 'scope' ->> 'calibre'), '')) as e_cal,
         case p.purpose when 'assess_tender' then array['award', 'contract', 'tender']
                        when 'find_suppliers' then array['contract', 'award']
                        when 'evaluate_quote' then array['contract', 'award', 'conversation']
                        when 'search' then nullif(array(select jsonb_array_elements_text(coalesce(p.subject -> 'kinds', '[]'::jsonb))), '{{}}'::text[])
         end as e_kinds,
         case when p.purpose = 'search'
              then nullif(regexp_replace(coalesce(nullif(btrim(p.subject ->> 'cpv_prefix'), ''), ''), '[^0-9]', '', 'g'), '') end as e_cpv,
         case when p.purpose = 'search' then nullif(p.pack -> 'scope' ->> 'country', '') end as e_country,
         case when p.pack -> 'scope' ->> 'publication_number' is not null
              then array['ted:' || (p.pack -> 'scope' ->> 'publication_number')] else '{{}}'::text[] end as e_excl,
         8 as e_lim,
         least(greatest(8 + case when p.pack -> 'scope' ->> 'publication_number' is not null then 1 else 0 end, 1), 50) as e_klimit,
         case p.purpose when 'assess_tender' then true
                        when 'find_suppliers' then coalesce(p.pack -> 'scope' ->> 'family_id', p.pack -> 'scope' ->> 'class_id') is not null
                                                   or nullif(btrim(p.query_text), '') is not null or p.emb is not null
                        when 'evaluate_quote' then nullif(btrim(p.query_text), '') is not null or p.emb is not null
                        when 'search' then true else false end as e_do,
         nullif(p.subject ->> 'supplier_id', '') as c_sup,
         case when nullif(p.subject ->> 'supplier_id', '') is null then nullif(p.subject ->> 'buyer_id', '') end as c_buy
  from packs p
),
ev as (
  select pk.id, pk.mode, v.nobase,
         case when pk.purpose = 'check_counterparty' then {COMPANY_REPLICA}
              when pk.purpose = 'evaluate_quote_emulated' or not pk.e_do then '[]'::jsonb
              else {KB_SEARCH_REPLICA} end as items
  from pk cross join (values (false), (true)) v(nobase)
),
variants as (
  select pk.id, pk.mode, pk.purpose, pk.question, pk.expected, pk.pack,
         (select jsonb_agg(x ->> 'ref' order by o) from jsonb_array_elements(pk.pack -> 'evidence') with ordinality t(x, o)) as refs_pack,
         (select jsonb_agg(x ->> 'ref' order by o) from jsonb_array_elements(e0.items) with ordinality t(x, o)) as refs_replica,
         pk.pack || jsonb_build_object(
           'evidence', e1.items,
           'priors', coalesce(pk.pack -> 'priors', '{{}}'::jsonb) || jsonb_build_object(
                       'baseline', null,
                       'blend', case when pk.pack -> 'priors' -> 'blend' ->> 'basis' = 'baseline_only' then null
                                     else pk.pack -> 'priors' -> 'blend' end),
           'facts', case when pk.pack -> 'facts' -> 'quote' is null or jsonb_typeof(pk.pack -> 'facts' -> 'quote') <> 'object'
                         then pk.pack -> 'facts'
                         else jsonb_set(pk.pack -> 'facts', '{{quote,benchmark}}', coalesce({benchmark(
                                "(pk.pack -> 'facts' -> 'quote')", "(pk.pack -> 'price_anchors')",
                                "((pk.pack -> 'priors') || jsonb_build_object('baseline', null, 'blend', null))")}, 'null'::jsonb)) end
         ) as pack_nohist
  from pk
  join ev e0 on e0.id = pk.id and e0.mode = pk.mode and not e0.nobase
  join ev e1 on e1.id = pk.id and e1.mode = pk.mode and e1.nobase
),
scored as (
  select v.*,
         lower(jsonb_build_array(v.pack -> 'facts', v.pack -> 'candidates', v.pack -> 'evidence', v.pack -> 'price_anchors',
                                 v.pack -> 'priors', v.pack -> 'regulatory', v.pack -> 'memory', v.pack -> 'warnings')::text) as t_with,
         lower(jsonb_build_array(v.pack_nohist -> 'facts', v.pack_nohist -> 'candidates', v.pack_nohist -> 'evidence',
                                 v.pack_nohist -> 'price_anchors', v.pack_nohist -> 'priors', v.pack_nohist -> 'regulatory',
                                 v.pack_nohist -> 'memory', v.pack_nohist -> 'warnings')::text) as t_without
  from variants v
),
items as (
  select s.id, s.mode, e.o, e.x ->> 'label' as label, coalesce(e.x ->> 'type', 'text') as typ,
         case coalesce(e.x ->> 'type', 'text')
           when 'text' then s.t_with ~* (e.x ->> 're')
           when 'warning' then lower((s.pack -> 'warnings')::text) ~* (e.x ->> 're')
           when 'number' then coalesce(public.agent_ctx_num(s.pack #> array(select jsonb_array_elements_text(e.x -> 'path')))
                                       between (e.x ->> 'value')::numeric - (e.x ->> 'tol')::numeric
                                           and (e.x ->> 'value')::numeric + (e.x ->> 'tol')::numeric, false)
         end as hit_with,
         case coalesce(e.x ->> 'type', 'text')
           when 'text' then s.t_without ~* (e.x ->> 're')
           when 'warning' then lower((s.pack_nohist -> 'warnings')::text) ~* (e.x ->> 're')
           when 'number' then coalesce(public.agent_ctx_num(s.pack_nohist #> array(select jsonb_array_elements_text(e.x -> 'path')))
                                       between (e.x ->> 'value')::numeric - (e.x ->> 'tol')::numeric
                                           and (e.x ->> 'value')::numeric + (e.x ->> 'tol')::numeric, false)
         end as hit_without,
         case when coalesce(e.x ->> 'type', 'text') = 'text' then
           array(select sec from unnest(array['facts', 'candidates', 'evidence', 'price_anchors', 'priors', 'memory', 'warnings']) sec
                 where lower((s.pack -> sec)::text) ~* (e.x ->> 're'))
         else array[split_part(coalesce(e.x -> 'path' ->> 0, 'warnings'), '.', 1)] end as sections
  from scored s cross join lateral jsonb_array_elements(coalesce(s.expected, '[]'::jsonb)) with ordinality e(x, o)
),
nums as (   -- every unit-price-like number the agent would see, with the family it applies to
  select s.id, s.mode, n.src, n.fam, n.val, upper(n.cur) as cur
  from scored s cross join lateral (
    select 'priors.baseline.unit_price_' || k as src, coalesce(s.pack -> 'priors' -> 'baseline' ->> 'family_id', s.pack -> 'scope' ->> 'family_id') as fam,
           public.agent_ctx_num(s.pack -> 'priors' -> 'baseline' -> ('unit_price_' || k)) as val, s.pack -> 'priors' -> 'baseline' ->> 'currency' as cur
    from unnest(array['p25', 'p50', 'p75']) k
    union all
    select 'facts.quote.benchmark.' || k, s.pack -> 'scope' ->> 'family_id',
           public.agent_ctx_num(s.pack -> 'facts' -> 'quote' -> 'benchmark' -> k), s.pack -> 'facts' -> 'quote' -> 'benchmark' ->> 'currency'
    from unnest(array['p25', 'p50', 'p75']) k
    where s.pack -> 'facts' -> 'quote' -> 'benchmark' ->> 'basis' in ('anchor_unit_prices', 'baseline_unit_price')
    union all
    select 'price_anchors[' || (o - 1) || '].unit_price', x ->> 'family_id', public.agent_ctx_num(x -> 'unit_price'), x ->> 'currency'
    from jsonb_array_elements(coalesce(s.pack -> 'price_anchors', '[]'::jsonb)) with ordinality t(x, o)
    union all
    select 'evidence[' || (o - 1) || '].facts.unit_price (' || (x ->> 'ref') || ')', x -> 'facts' ->> 'family_id',
           public.agent_ctx_num(x -> 'facts' -> 'unit_price'), x -> 'facts' ->> 'currency'
    from jsonb_array_elements(coalesce(s.pack -> 'evidence', '[]'::jsonb)) with ordinality t(x, o)
  ) n
  where n.val is not null
),
wrong as (
  select s.id, s.mode, w.msg
  from scored s cross join lateral (
    select format('%s = %s %s (~%s EUR) outside the %s-%s EUR band assumed for %s', n.src, round(n.val, 4), n.cur,
                  round(n.val * fx.eur, 2), b.lo_eur, b.hi_eur, n.fam) as msg
    from nums n join fx on fx.cur = n.cur join band b on b.family_id = n.fam
    where n.id = s.id and n.mode = s.mode and (n.val * fx.eur < b.lo_eur or n.val * fx.eur > b.hi_eur)
    union all
    select format('%s = %s %s per unit (no band for %s; above 1,000,000)', n.src, round(n.val, 2), n.cur, coalesce(n.fam, 'unknown family'))
    from nums n left join band b on b.family_id = n.fam
    where n.id = s.id and n.mode = s.mode and b.family_id is null and n.val > 1000000
    union all
    select format('priors.baseline unit price spread p75/p25 = %s (n_unit_prices %s)',
                  round((s.pack -> 'priors' -> 'baseline' ->> 'unit_price_p75')::numeric
                        / nullif((s.pack -> 'priors' -> 'baseline' ->> 'unit_price_p25')::numeric, 0), 1),
                  s.pack -> 'priors' -> 'baseline' ->> 'n_unit_prices')
    where (s.pack -> 'priors' -> 'baseline' ->> 'unit_price_p75')::numeric
          > 20 * nullif((s.pack -> 'priors' -> 'baseline' ->> 'unit_price_p25')::numeric, 0)
    union all
    select format('priors.baseline.awarded_value_p50 = %s (not positive)', s.pack -> 'priors' -> 'baseline' ->> 'awarded_value_p50')
    where (s.pack -> 'priors' -> 'baseline' ->> 'awarded_value_p50')::numeric <= 0
    union all
    select 'priors.baseline.bids_median = 0 (an awarded procedure has at least one bid: unknown stored as 0?)'
    where (s.pack -> 'priors' -> 'baseline' ->> 'bids_median')::numeric = 0
    union all
    select format('price_anchors: %s item(s) with value <= 0', count(*))
    from jsonb_array_elements(coalesce(s.pack -> 'price_anchors', '[]'::jsonb)) x
    where public.agent_ctx_num(x -> 'value') <= 0 having count(*) > 0
    union all
    select format('evidence: %s card(s) with a non-positive value (%s)', count(*), string_agg(x ->> 'ref', ', '))
    from jsonb_array_elements(coalesce(s.pack -> 'evidence', '[]'::jsonb)) x
    where coalesce(public.agent_ctx_num(x -> 'facts' -> 'notice_value'), public.agent_ctx_num(x -> 'facts' -> 'awarded_value'), public.agent_ctx_num(x -> 'facts' -> 'value')) <= 0
    having count(*) > 0
    union all
    select format('evidence: %s awarded card(s) with bids_received = 0 (%s)', count(*), string_agg(x ->> 'ref', ', '))
    from jsonb_array_elements(coalesce(s.pack -> 'evidence', '[]'::jsonb)) x
    where public.agent_ctx_num(x -> 'facts' -> 'bids_received') = 0
      and (x ->> 'kind' = 'award' or x -> 'facts' ->> 'outcome' = 'awarded')
    having count(*) > 0
  ) w
),
per as (
  select s.id, s.mode, s.purpose, s.question,
         (select count(*) from items i where i.id = s.id and i.mode = s.mode) as n_exp,
         (select count(*) filter (where i.hit_with) from items i where i.id = s.id and i.mode = s.mode) as n_with,
         (select count(*) filter (where i.hit_without) from items i where i.id = s.id and i.mode = s.mode) as n_without,
         (select string_agg(i.label, '; ' order by i.o) from items i where i.id = s.id and i.mode = s.mode and i.hit_with and not i.hit_without) as only_hist,
         (select string_agg(i.label, '; ' order by i.o) from items i where i.id = s.id and i.mode = s.mode and not i.hit_with) as missed,
         (select string_agg(distinct sec, ',') from items i, unnest(i.sections) sec where i.id = s.id and i.mode = s.mode and i.hit_with) as sections,
         jsonb_array_length(coalesce(s.pack -> 'warnings', '[]'::jsonb)) as n_warn,
         (select string_agg(left(w, 110), ' / ') from (select jsonb_array_elements_text(s.pack -> 'warnings') w limit 3) z) as warn3,
         length(s.pack::text) as chars_with, length(s.pack_nohist::text) as chars_without,
         (s.pack -> 'versions' ->> 'elapsed_ms')::numeric as ms,
         (select string_agg(l || ':' || n, ' ' order by l) from (select x ->> 'layer' as l, count(*) n from jsonb_array_elements(coalesce(s.pack -> 'evidence', '[]'::jsonb)) x group by 1) z) as ev_with,
         (select string_agg(l || ':' || n, ' ' order by l) from (select x ->> 'layer' as l, count(*) n from jsonb_array_elements(coalesce(s.pack_nohist -> 'evidence', '[]'::jsonb)) x group by 1) z) as ev_without,
         (select count(*) - count(distinct substring(x ->> 'ref' from '([0-9]+-20[0-9][0-9])'))
          from jsonb_array_elements(coalesce(s.pack -> 'evidence', '[]'::jsonb)) x
          where x ->> 'ref' ~ '[0-9]+-20[0-9][0-9]') as dup_notices,
         case when s.purpose = 'evaluate_quote_emulated' then 'n/a'
              when coalesce(s.refs_pack, '[]'::jsonb) = coalesce(s.refs_replica, '[]'::jsonb) then 'exact' else 'DIFF' end as fidelity,
         s.pack -> 'priors' -> 'baseline' is not null and jsonb_typeof(s.pack -> 'priors' -> 'baseline') = 'object' as has_baseline,
         s.pack -> 'priors' -> 'baseline' ->> 'scope_level' as baseline_scope,
         s.pack -> 'facts' -> 'quote' -> 'benchmark' as bm_with, s.pack_nohist -> 'facts' -> 'quote' -> 'benchmark' as bm_without,
         (select count(*) from wrong w where w.id = s.id and w.mode = s.mode) as n_wrong
  from scored s
),
lines as (
  select 0 as sec, 0 as o1, ''::text as o2, n::int as o3, line from unnest(array[
    ('# agent_context evaluation: does the historical layer help?'),
    (''),
    (format('Run at %s UTC. Active snapshot: %s (frozen %s, cutoff %s). kb version %s. Questions: %s, pack rows: %s.',
            to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI'),
            (select string_agg(s.id, ', ') from public.kb_snapshots s where s.active),
            (select to_char(max(s.frozen_at) at time zone 'utc', 'YYYY-MM-DD HH24:MI') from public.kb_snapshots s where s.active),
            (select max(s.cutoff_date)::text from public.kb_snapshots s where s.active),
            (select to_char(max(k.updated_at) at time zone 'utc', 'YYYY-MM-DD HH24:MI') from public.kb_chunks k where k.layer <> 'baseline'),
            (select count(*) from q), (select count(*) from per))),
    (''),
    ('"with" = the pack as agent_context returns it. "without" = the same pack with priors.baseline (and a baseline-only blend) removed and the evidence list recomputed by an exact replica of kb_search / agent_ctx_company_chunks that skips layer = baseline. Fidelity "exact" means the replica, run with the baseline, returned the same evidence refs as the real pack.'),
    (''),
    ('## Summary by purpose and mode'),
    (''),
    ('| purpose | mode | questions | recall with history | recall without | packs with priors.baseline | wrong numbers | median chars (~tokens) | median / max ms |'),
    ('|---|---|---|---|---|---|---|---|---|')]) with ordinality v(line, n)
  union all
  select 1, case p.purpose when 'assess_tender' then 1 when 'find_suppliers' then 2 when 'evaluate_quote' then 3
                           when 'evaluate_quote_emulated' then 4 when 'check_counterparty' then 5 else 6 end, p.mode, 0,
         format('| %s | %s | %s | %s%% (%s/%s) | %s%% (%s/%s) | %s/%s | %s | %s (~%s) | %s |', p.purpose, p.mode, count(*),
                round(100.0 * sum(p.n_with) / nullif(sum(p.n_exp), 0)), sum(p.n_with), sum(p.n_exp),
                round(100.0 * sum(p.n_without) / nullif(sum(p.n_exp), 0)), sum(p.n_without), sum(p.n_exp),
                count(*) filter (where p.has_baseline), count(*), sum(p.n_wrong),
                percentile_disc(0.5) within group (order by p.chars_with), percentile_disc(0.5) within group (order by p.chars_with) / 4,
                coalesce(round(percentile_cont(0.5) within group (order by p.ms)) || ' / ' || round(max(p.ms)), 'n/a'))
  from per p group by p.purpose, p.mode
  union all
  select 2, 0, p.mode, 0,
         format('| **all** | %s | %s | **%s%%** (%s/%s) | **%s%%** (%s/%s) | %s/%s | %s | %s (~%s) | %s |', p.mode, count(*),
                round(100.0 * sum(p.n_with) / nullif(sum(p.n_exp), 0)), sum(p.n_with), sum(p.n_exp),
                round(100.0 * sum(p.n_without) / nullif(sum(p.n_exp), 0)), sum(p.n_without), sum(p.n_exp),
                count(*) filter (where p.has_baseline), count(*), sum(p.n_wrong),
                percentile_disc(0.5) within group (order by p.chars_with), percentile_disc(0.5) within group (order by p.chars_with) / 4,
                coalesce(round(percentile_cont(0.5) within group (order by p.ms)) || ' / ' || round(max(p.ms)), 'n/a'))
  from per p group by p.mode
  union all
  select 3, 0, '', n::int, line from unnest(array[(''), ('## Per question'), (''),
    ('| id | purpose | mode | recall with | recall without | found only with history | answered from | warnings | chars with/without | ms | evidence layers with -> without | same notice twice | fidelity | wrong numbers |'),
    ('|---|---|---|---|---|---|---|---|---|---|---|---|---|---|')]) with ordinality v(line, n)
  union all
  select 4, 0, p.id || p.mode, 0,
         format('| %s | %s | %s | %s/%s | %s/%s | %s | %s | %s | %s/%s | %s | %s -> %s | %s | %s | %s |', p.id, p.purpose, p.mode,
                p.n_with, p.n_exp, p.n_without, p.n_exp, coalesce(p.only_hist, ''), coalesce(p.sections, ''), p.n_warn,
                p.chars_with, p.chars_without, coalesce(p.ms::text, 'n/a'), coalesce(p.ev_with, '-'), coalesce(p.ev_without, '-'),
                p.dup_notices, p.fidelity, p.n_wrong)
  from per p
  union all
  select 5, 0, '', n::int, line from unnest(array[(''), ('## Missed items, priors scope, quote benchmark, first warnings'), ('')]) with ordinality v(line, n)
  union all
  select 6, 0, p.id || p.mode, 0,
         format('- **%s %s** (%s): %s Missed: %s. Baseline prior scope: %s.%s Warnings: %s', p.id, p.mode, p.purpose, p.question,
                coalesce(p.missed, 'none'), coalesce(p.baseline_scope, 'none'),
                case when p.bm_with is not null or p.bm_without is not null
                     then format(' Benchmark with: %s; without: %s.', coalesce(p.bm_with::text, 'none'), coalesce(p.bm_without::text, 'none')) else '' end,
                coalesce(p.warn3, 'none'))
  from per p
  union all
  select 7, 0, '', n::int, line from unnest(array[(''), ('## Wrong or misleading numbers in the packs (with history)'), ('')]) with ordinality v(line, n)
  union all
  select 8, 0, w.id || w.mode, row_number() over (partition by w.id, w.mode order by w.msg)::int,
         format('- %s %s: %s', w.id, w.mode, w.msg)
  from wrong w
)
select line from lines order by sec, o1, o2, o3;
"""
    return sql


def main():
    qpath = HERE / "questions.json"
    questions = json.loads(qpath.read_text(encoding="utf-8"))
    stale = [q["id"] for q in questions if q.get("ground_truth_sql") != render(q["id"], q["ground_truth"])]
    if stale:
        if "--check" in sys.argv:
            sys.exit(f"ground_truth_sql is stale for {stale}: run build_eval.py --write-questions")
        if "--write-questions" in sys.argv:
            for q in questions:
                q["ground_truth_sql"] = render(q["id"], q["ground_truth"])
            qpath.write_text(json.dumps(questions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    only = next((a.split("=", 1)[1].split(",") for a in sys.argv if a.startswith("--only=")), None)
    if only:   # debugging a few questions: python3 build_eval.py --only=Q08,Q22 > /tmp/subset.sql
        sys.stdout.write(eval_sql([q for q in questions if q["id"] in only], "subset " + ",".join(only)))
        return
    (HERE / "ground_truth.sql").write_text(ground_truth_sql(questions), encoding="utf-8")
    (HERE / "eval.sql").write_text(eval_sql(questions, f"{len(questions)} questions"), encoding="utf-8")
    print(f"wrote ground_truth.sql and eval.sql for {len(questions)} questions")


if __name__ == "__main__":
    main()
