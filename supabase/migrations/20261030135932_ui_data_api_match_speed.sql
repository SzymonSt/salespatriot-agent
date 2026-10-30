-- UI data API, speed (Agent 4): ui_match_tender computes the company name key once per row (only
-- for rows without supplier_id) and hash-joins the materialised supplier name keys instead of a
-- correlated subquery; ui_supplier_families computes the key only for rows without supplier_id.
-- Same columns and results.

-- ---------------------------------------------------------------------------------------------
-- ui_supplier_families: what each known supplier (suppliers.id) has proven or declared per family.
-- proven_lots = distinct TED lots from supplier_contracts plus ted_awards notices not already
-- covered by a supplier_contracts lot of the same notice (no double counting).
-- best_evidence = best verification_status among the items
-- (independently_verified > document_verified > peer_attested > declared > inferred).
-- Items count for a supplier when supplier_id is set or, failing that, when org_ref has the same
-- name key (ui_name_key) as suppliers.name / legal_name (awards the bridge did not link).
-- Excludes out_of_scope.noise. Trailing extras: in_focus, item_count.
-- ---------------------------------------------------------------------------------------------
create or replace view public.ui_supplier_families
with (security_invoker = on) as
with spk as (
  select min(x.id) as id, x.k
  from (select sp.id, public.ui_name_key(sp.name) as k from public.suppliers sp
        union all
        select sp.id, public.ui_name_key(sp.legal_name) from public.suppliers sp) x
  where x.k is not null
  group by x.k
),
s as (
  select
    i.id,
    coalesce(i.supplier_id, spk.id) as supplier_id,
    i.family_id,
    i.source_table,
    i.publication_number,
    i.lot_id,
    i.observed_at,
    i.verification_status,
    nullif(btrim(i.attrs ->> 'calibre'), '') as calibre,
    bool_or(i.source_table = 'supplier_contracts')
      over (partition by coalesce(i.supplier_id, spk.id), i.family_id, i.publication_number) as pub_has_contract
  from public.product_items i
  left join spk on spk.k = case when i.supplier_id is null then public.ui_name_key(i.org_ref) end
  where i.side = 'supply'
    and i.visibility = 'public'
    and coalesce(i.supplier_id, spk.id) is not null
    and i.family_id is not null
    and i.family_id <> 'out_of_scope.noise'
),
k as (
  select
    s.*,
    case
      when s.source_table = 'supplier_contracts'
        then s.publication_number || '/' || coalesce(s.lot_id, 'notice')
      when s.source_table = 'ted_awards' and not s.pub_has_contract
        then s.publication_number || '/award'
    end as lot_key,
    case s.verification_status
      when 'independently_verified' then 0 when 'document_verified' then 1
      when 'peer_attested' then 2 when 'declared' then 3 when 'inferred' then 4 else 5
    end as ev_rank
  from s
)
select
  k.supplier_id,
  k.family_id,
  f.name                                                                   as family_name,
  f.parent_id                                                              as class_id,
  c.name                                                                   as class_name,
  count(distinct k.lot_key)::int                                           as proven_lots,
  (count(*) filter (where k.source_table = 'suppliers.catalogue'))::int    as declared_items,
  coalesce(array_agg(distinct k.calibre) filter (where k.calibre is not null), '{}'::text[]) as calibres,
  (max(k.observed_at) filter (where k.lot_key is not null))::date          as last_award,
  (array_agg(k.verification_status order by k.ev_rank)
     filter (where k.verification_status is not null))[1]                  as best_evidence,
  f.in_focus,
  count(*)::int                                                            as item_count
from k
join public.product_families f on f.id = k.family_id
left join public.product_families c on c.id = f.parent_id
group by k.supplier_id, k.family_id, f.name, f.parent_id, c.name, f.in_focus;

comment on view public.ui_supplier_families is
  'UI read API: per supplier and family, proven TED lots, declared catalogue items, calibres, last award, best evidence.';

create or replace function public.ui_match_tender(p_publication_number text, p_limit int default 10)
returns table (
  supplier_id    text,
  org_ref        text,
  org_name       text,
  family_id      text,
  family_name    text,
  calibre        text,
  key_matches    int,
  proven_lots    int,
  declared_items int,
  best_evidence  text,
  reasons        text[]
)
language sql
stable
security invoker
set search_path = ''
as $$
with
d as (
  select i.family_id, i.class_id, coalesce(i.attrs, '{}'::jsonb) as attrs,
         nullif(btrim(i.attrs ->> 'calibre'), '') as calibre
  from public.product_items i
  where i.publication_number = p_publication_number
    and i.side = 'demand'
    and i.visibility = 'public'
),
spk as materialized (
  -- name keys of the known suppliers, to attach awards whose winner was not linked to a supplier
  select min(x.id) as id, x.k
  from (select sp.id, public.ui_name_key(sp.name) as k from public.suppliers sp
        union all
        select sp.id, public.ui_name_key(sp.legal_name) from public.suppliers sp) x
  where x.k is not null
  group by x.k
),
tb as (
  -- the tender's buyer, to spot companies that already delivered to it
  select nullif(btrim(n.buyer_identifier), '') as ident, lower(nullif(btrim(n.buyer_name), '')) as name
  from public.ted_notices n
  where n.publication_number = p_publication_number
),
md as (
  select case
    when exists (select 1 from d join public.product_families f on f.id = d.family_id where f.in_focus) then 'family'
    when exists (select 1 from d where d.family_id is not null) then 'none'
    when exists (select 1 from d join public.product_families c on c.id = d.class_id where c.in_focus) then 'class'
    when exists (select 1 from d where d.class_id is not null) then 'none'
    else 'cpv'
  end as kind
),
t as (
  -- family level: one target per demand item with an in-focus family
  select 'family'::text as lvl, d.family_id, f.parent_id as class_id, d.calibre, d.attrs,
         array(select k.attr_key
               from public.product_family_attribute_list(d.family_id) k
               where k.is_match_key and nullif(btrim(d.attrs ->> k.attr_key), '') is not null) as keys
  from d
  join public.product_families f on f.id = d.family_id
  where f.in_focus and (select md.kind from md) = 'family'
  union all
  -- class level: classified to an in-focus class but not (yet) to a family
  select 'class', null, d.class_id, d.calibre, d.attrs,
         case when d.calibre is not null then array['calibre'] else '{}'::text[] end
  from d
  join public.product_families c on c.id = d.class_id
  where c.in_focus and d.family_id is null and (select md.kind from md) = 'class'
  union all
  -- CPV level: tender not classified yet; class from the notice CPV codes (first 4 digits)
  select distinct 'cpv', null::text, cw.family_id, null::text, '{}'::jsonb, '{}'::text[]
  from public.ted_notices n
  cross join lateral unnest(n.cpv_codes) as cpv(code)
  join public.product_family_crosswalk cw on cw.scheme = 'cpv' and left(cw.code, 4) = left(cpv.code, 4)
  join public.product_families f on f.id = cw.family_id and f.level = 1 and f.in_focus
  where n.publication_number = p_publication_number
    and (select md.kind from md) = 'cpv'
    and not exists (select 1 from public.product_noise_rules nr where nr.cpv_code = cpv.code)
),
m as (
  select
    t.lvl,
    t.class_id                                                      as t_class,
    t.calibre                                                       as d_cal,
    s.id,
    coalesce(s.supplier_id, spk.id, 'org:' || x.nk)                as company,
    coalesce(s.supplier_id, spk.id)                                 as supplier_id,
    s.org_ref,
    s.family_id,
    s.source_table,
    s.publication_number,
    s.lot_id,
    s.observed_at,
    s.verification_status,
    x.s_cal,
    case when t.calibre is null then 2
         when x.s_cal is null then 3
         when x.s_cal = t.calibre then 0
         else 1 end                                                 as cal_rank,
    (select count(*)
     from unnest(t.keys) as kk(key)
     where (kk.key = 'calibre' and x.s_cal is not null)
        or (kk.key <> 'calibre' and s.attrs ->> kk.key = t.attrs ->> kk.key))::int as key_matches,
    case when s.source_table in ('supplier_contracts', 'ted_awards') then
      exists (select 1 from public.ted_notices rn, tb
              where rn.publication_number = s.publication_number
                and ((tb.ident is not null and rn.buyer_identifier = tb.ident)
                     or lower(btrim(rn.buyer_name)) = tb.name))
      or (s.source_table = 'supplier_contracts' and exists (
              select 1 from public.supplier_contracts sc, tb
              where sc.supplier_id = coalesce(s.supplier_id, spk.id) and sc.notice_number = s.publication_number
                and lower(btrim(sc.buyer)) = tb.name))
    else false end                                                  as same_buyer
  from t
  join public.product_items s
    on s.side = 'supply'
   and s.visibility = 'public'
   and ((t.lvl = 'family' and s.family_id = t.family_id) or (t.lvl <> 'family' and s.class_id = t.class_id))
  cross join lateral (select nullif(btrim(s.attrs ->> 'calibre'), '') as s_cal,
                             case when s.supplier_id is null then public.ui_name_key(s.org_ref) end as nk) x
  left join spk on spk.k = x.nk
  where coalesce(s.supplier_id, x.nk) is not null
    and (t.lvl = 'family' or s.family_id is null
         or exists (select 1 from public.product_families sf where sf.id = s.family_id and sf.in_focus))
    and (t.calibre is null or x.s_cal is null or x.s_cal = t.calibre
         or exists (select 1 from public.product_calibres pc
                    where (pc.code = t.calibre and x.s_cal = any (pc.related_codes))
                       or (pc.code = x.s_cal and t.calibre = any (pc.related_codes))))
),
l as (
  select
    m.*,
    case
      when m.source_table = 'supplier_contracts'
        then m.publication_number || '/' || coalesce(m.lot_id, 'notice')
      when m.source_table = 'ted_awards'
       and not bool_or(m.source_table = 'supplier_contracts')
               over (partition by m.company, m.publication_number)
        then m.publication_number || '/award'
    end as lot_key,
    case m.verification_status
      when 'independently_verified' then 0 when 'document_verified' then 1
      when 'peer_attested' then 2 when 'declared' then 3 when 'inferred' then 4 else 5
    end as ev_rank
  from m
),
a as (
  select
    l.company,
    max(l.supplier_id)                                                         as supplier_id,
    mode() within group (order by l.org_ref)                                   as org_ref,
    max(l.lvl)                                                                 as lvl,
    max(l.t_class)                                                             as t_class,
    max(l.d_cal)                                                               as d_cal,
    max(l.key_matches)                                                         as key_matches,
    count(distinct l.lot_key)::int                                             as proven_lots,
    (count(distinct l.lot_key) filter (where l.cal_rank = 0))::int             as proven_exact,
    (count(distinct l.lot_key) filter (where l.cal_rank = 1))::int             as proven_related,
    (count(distinct l.lot_key) filter (where l.same_buyer))::int               as buyer_lots,
    (count(distinct l.id) filter (where l.source_table = 'suppliers.catalogue'))::int as declared_items,
    (count(distinct l.id) filter (where l.source_table = 'suppliers.catalogue' and l.cal_rank = 0))::int as declared_exact,
    (count(distinct l.id) filter (where l.source_table = 'suppliers.catalogue' and l.cal_rank = 1))::int as declared_related,
    min(l.ev_rank)                                                             as ev_rank,
    min(l.cal_rank)                                                            as cal_rank,
    (array_agg(l.family_id order by l.key_matches desc, l.cal_rank, (l.lot_key is null), l.ev_rank)
       filter (where l.family_id is not null))[1]                              as family_id,
    (array_agg(l.s_cal order by l.cal_rank, l.ev_rank)
       filter (where l.s_cal is not null and l.cal_rank = 1))[1]               as related_cal,
    max(l.observed_at) filter (where l.lot_key is not null)                    as last_award
  from l
  group by l.company
)
select
  a.supplier_id,
  a.org_ref,
  coalesce(sp.name, a.org_ref)                                               as org_name,
  a.family_id,
  f.name                                                                     as family_name,
  case a.cal_rank when 0 then a.d_cal when 1 then a.related_cal end          as calibre,
  a.key_matches,
  a.proven_lots,
  a.declared_items,
  case a.ev_rank
    when 0 then 'independently_verified' when 1 then 'document_verified'
    when 2 then 'peer_attested' when 3 then 'declared' when 4 then 'inferred'
  end                                                                        as best_evidence,
  array_remove(array[
    case a.lvl
      when 'family' then 'Same family: ' || f.name
      when 'class'  then 'Same class: ' || cl.name || ' (tender not yet classified to a family)'
      when 'cpv'    then 'Same CPV class: ' || cl.name || ' (tender not classified yet)'
    end,
    case a.cal_rank
      when 0 then 'Calibre ' || a.d_cal || ' matches'
      when 1 then 'Calibre ' || a.related_cal || ' is related to ' || a.d_cal
                  || ' (compatible, check the specification)'
      when 3 then 'Calibre ' || a.d_cal || ' not stated on this company''s items'
    end,
    case when a.proven_lots > 0 then
      a.proven_lots || ' TED contract' || case when a.proven_lots = 1 then '' else 's' end
      || ' in this ' || case when a.lvl = 'family' then 'family' else 'class' end
      || case when a.proven_exact > 0 and a.proven_related > 0
                then ' (' || a.proven_exact || ' in ' || a.d_cal || ', ' || a.proven_related || ' in a related calibre)'
              when a.proven_exact > 0 then ' (' || a.proven_exact || ' in ' || a.d_cal || ')'
              when a.proven_related > 0 then ' (' || a.proven_related || ' in a related calibre)'
              else '' end
    end,
    case when a.buyer_lots > 0 then
      'Already delivered to this buyer (' || a.buyer_lots || ' TED contract'
      || case when a.buyer_lots = 1 then '' else 's' end || ')'
    end,
    case
      when a.declared_exact > 0 then 'Catalogue lists this calibre'
      when a.declared_related > 0 then 'Catalogue lists a related calibre'
      when a.declared_items > 0 then
        'Catalogue lists ' || a.declared_items || ' item' || case when a.declared_items = 1 then '' else 's' end
        || ' in this ' || case when a.lvl = 'family' then 'family' else 'class' end
    end,
    case when a.last_award is not null then 'Last TED contract ' || to_char(a.last_award, 'YYYY-MM-DD') end,
    case when k.kyc_status is not null then
      'KYC ' || k.kyc_status
      || coalesce(', checked ' || to_char(k.kyc_checked_at, 'YYYY-MM-DD'), '')
      || case when k.refresh_due < current_date then ', refresh overdue' else '' end
    end
  ], null)                                                                   as reasons
from a
left join public.suppliers sp on sp.id = a.supplier_id
left join public.product_families f on f.id = a.family_id
left join public.product_families cl on cl.id = a.t_class
left join public.supplier_kyc k on k.supplier_id = a.supplier_id
order by a.key_matches desc, a.proven_lots desc, a.declared_items desc, a.ev_rank,
         coalesce(sp.name, a.org_ref)
limit greatest(1, least(coalesce(p_limit, 10), 100));
$$;

comment on function public.ui_match_tender(text, int) is
  'UI read API: companies fitting a TED tender (family + calibre), one row per company, with plain-English reasons.';

grant select on public.ui_supplier_families to anon, authenticated;
grant execute on function public.ui_match_tender(text, int) to anon, authenticated;
