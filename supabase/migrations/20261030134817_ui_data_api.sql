-- UI data API (Agent 4): read-only views and functions the web UI calls through PostgREST.
-- Contract: scratchpad merge_contract.md, section "UI data API". All objects are security invoker,
-- read only public items (visibility = 'public') and are granted to anon and authenticated.
-- Ontology rows are never changed here.

-- ---------------------------------------------------------------------------------------------
-- ui_items: one row per public product item, with family and class names.
-- Trailing extras beyond the contract: source_kind, source_ref, observed_at, quantity, unit, cpv_codes.
-- ---------------------------------------------------------------------------------------------
create or replace view public.ui_items
with (security_invoker = on) as
select
  i.id,
  i.side,
  i.source_table,
  i.source_pk,
  i.source_index,
  i.supplier_id,
  i.buyer_id,
  i.publication_number,
  i.lot_id,
  i.org_ref,
  i.title,
  i.class_id,
  c.name                                  as class_name,
  i.family_id,
  f.name                                  as family_name,
  i.attrs,
  nullif(btrim(i.attrs ->> 'calibre'), '') as calibre,
  i.classification_method,
  i.confidence,
  i.needs_review,
  i.verification_status,
  i.source_kind,
  i.source_ref,
  i.observed_at,
  i.quantity,
  i.unit,
  i.cpv_codes
from public.product_items i
left join public.product_families c on c.id = i.class_id
left join public.product_families f on f.id = i.family_id
where i.visibility = 'public';

comment on view public.ui_items is
  'UI read API: public product items with class and family names; calibre = attrs->>calibre.';

-- ---------------------------------------------------------------------------------------------
-- ui_supplier_families: what each known supplier (suppliers.id) has proven or declared per family.
-- proven_lots = distinct TED lots from supplier_contracts plus ted_awards notices not already
-- covered by a supplier_contracts lot of the same notice (no double counting).
-- best_evidence = best verification_status among the items
-- (independently_verified > document_verified > peer_attested > declared > inferred).
-- Excludes out_of_scope.noise. Trailing extras: in_focus, item_count.
-- ---------------------------------------------------------------------------------------------
create or replace view public.ui_supplier_families
with (security_invoker = on) as
with s as (
  select
    i.id,
    i.supplier_id,
    i.family_id,
    i.source_table,
    i.publication_number,
    i.lot_id,
    i.observed_at,
    i.verification_status,
    nullif(btrim(i.attrs ->> 'calibre'), '') as calibre,
    bool_or(i.source_table = 'supplier_contracts')
      over (partition by i.supplier_id, i.family_id, i.publication_number) as pub_has_contract
  from public.product_items i
  where i.side = 'supply'
    and i.visibility = 'public'
    and i.supplier_id is not null
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

-- ---------------------------------------------------------------------------------------------
-- ui_tender_families: families of each TED tender (demand items), with deadline and open flag.
-- deadline = greatest(deadline_tender, deadline_request); is_open = deadline >= now() (null if no deadline).
-- Excludes out_of_scope.noise. Trailing extras: class_name, in_focus.
-- ---------------------------------------------------------------------------------------------
create or replace view public.ui_tender_families
with (security_invoker = on) as
select
  i.publication_number,
  i.family_id,
  f.name                                                                   as family_name,
  f.parent_id                                                              as class_id,
  coalesce(array_agg(distinct nullif(btrim(i.attrs ->> 'calibre'), ''))
             filter (where nullif(btrim(i.attrs ->> 'calibre'), '') is not null), '{}'::text[]) as calibres,
  max(i.confidence)                                                        as confidence,
  (array_agg(i.classification_method order by i.confidence desc nulls last))[1] as classification_method,
  greatest(n.deadline_tender, n.deadline_request) >= now()                 as is_open,
  greatest(n.deadline_tender, n.deadline_request)                          as deadline,
  c.name                                                                   as class_name,
  f.in_focus
from public.product_items i
join public.product_families f on f.id = i.family_id
left join public.product_families c on c.id = f.parent_id
left join public.ted_notices n on n.publication_number = i.publication_number
where i.side = 'demand'
  and i.visibility = 'public'
  and i.publication_number is not null
  and i.family_id <> 'out_of_scope.noise'
group by i.publication_number, i.family_id, f.name, f.parent_id, c.name, f.in_focus,
         n.deadline_tender, n.deadline_request;

comment on view public.ui_tender_families is
  'UI read API: families and calibres per TED tender, with deadline and is_open.';

-- ---------------------------------------------------------------------------------------------
-- ui_match_tender: companies that fit a TED tender, one row per company, best first.
-- Demand items of the tender -> family (+ calibre) -> public supply items of the same family.
-- Calibre is a hard filter when both sides state it; product_calibres.related_codes count as
-- compatible (said in reasons). Fallbacks when the tender has no in-focus family yet:
-- 'class' (item has a class but no family) and 'cpv' (item not classified: class from the
-- notice CPV prefix). A tender classified out of scope returns no rows.
-- Order: key_matches, proven_lots, declared_items, evidence rank.
-- ---------------------------------------------------------------------------------------------
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
    coalesce(s.supplier_id, lower(btrim(s.org_ref)))                as company,
    s.supplier_id,
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
        or (kk.key <> 'calibre' and s.attrs ->> kk.key = t.attrs ->> kk.key))::int as key_matches
  from t
  join public.product_items s
    on s.side = 'supply'
   and s.visibility = 'public'
   and ((t.lvl = 'family' and s.family_id = t.family_id) or (t.lvl <> 'family' and s.class_id = t.class_id))
  cross join lateral (select nullif(btrim(s.attrs ->> 'calibre'), '') as s_cal) x
  where coalesce(s.supplier_id, nullif(btrim(s.org_ref), '')) is not null
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
    (count(distinct l.lot_key) filter (where l.cal_rank in (0, 1)))::int       as proven_in_cal,
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
      || case when a.proven_in_cal > 0 and a.d_cal is not null
              then ' (' || a.proven_in_cal || ' in ' || a.d_cal || ')' else '' end
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

-- ---------------------------------------------------------------------------------------------
-- ui_regulatory_profile: crosswalk rows of a family (and its class) by scheme, plus item rules
-- of KB 25 section 7 derived from the calibre: ml_by_calibre (weapons and parts),
-- fsc_by_bore (gun and ammunition families), adr_small_arms (cartridges and weapons <= 19.1 mm).
-- Returns null for an unknown family id.
-- ---------------------------------------------------------------------------------------------
create or replace function public.ui_regulatory_profile(p_family_id text, p_calibre text default null)
returns jsonb
language plpgsql
stable
security invoker
set search_path = ''
as $$
declare
  v_id        text;
  v_name      text;
  v_class     text;
  v_in_focus  boolean;
  v_schemes   jsonb;
  v_cal       text;
  v_label     text;
  v_kind      text;
  v_bore      numeric;
  v_standard  text;
  v_related   text[];
  v_known     boolean := false;
  v_smooth    boolean;
  v_ml        jsonb;
  v_fsc       jsonb;
  v_adr_sa    boolean;
  v_adr       jsonb;
  v_notes     text[] := '{}';
  v_code      text;
  v_title     text;
  v_has_ml    boolean;
begin
  select pf.id, pf.name, coalesce(pf.parent_id, pf.id), pf.in_focus
    into v_id, v_name, v_class, v_in_focus
  from public.product_families pf
  where pf.id = p_family_id;
  if v_id is null then
    return null;
  end if;

  -- crosswalk rows of the family and its class, grouped by scheme
  select coalesce(jsonb_object_agg(g.scheme, g.rows), '{}'::jsonb)
    into v_schemes
  from (
    select x.scheme,
           jsonb_agg(jsonb_build_object(
             'code', x.code, 'label', x.label, 'relation', x.relation, 'verified', x.verified,
             'note', x.note, 'source_url', x.source_url,
             'level', case when x.family_id = v_id then 'family' else 'class' end)
             order by (x.family_id <> v_id),
                      case x.relation when 'exact' then 0 when 'narrow' then 1 when 'candidate' then 2
                                      when 'related' then 3 else 4 end,
                      x.code) as rows
    from public.product_family_crosswalk x
    where x.family_id in (v_id, v_class)
      and x.scheme in ('eu_ml', 'fsc', 'hs', 'adr', 'fd_2021_555', 'stanag')
    group by x.scheme
  ) g;
  v_schemes := jsonb_build_object('eu_ml', '[]'::jsonb, 'fsc', '[]'::jsonb, 'hs', '[]'::jsonb,
                                  'adr', '[]'::jsonb, 'fd_2021_555', '[]'::jsonb, 'stanag', '[]'::jsonb)
               || v_schemes;
  v_has_ml := jsonb_array_length(v_schemes -> 'eu_ml') > 0
              and not (v_schemes -> 'eu_ml') @> '[{"code": "none"}]'::jsonb;

  -- calibre: normalise, look up bore; fall back to the bore in the code (5.56x45, 30mm)
  if nullif(btrim(p_calibre), '') is not null then
    v_cal := public.product_normalise_calibre(p_calibre);
    if v_cal is null then
      v_notes := v_notes || format('Calibre "%s" is not recognised; calibre rules not applied.', p_calibre);
    else
      select pc.label, pc.kind, pc.bore_mm, pc.standard, pc.related_codes
        into v_label, v_kind, v_bore, v_standard, v_related
      from public.product_calibres pc
      where pc.code = v_cal;
      v_known := found;
      if not v_known then
        v_bore := (regexp_match(v_cal, '^([0-9]{1,3}(\.[0-9]{1,2})?)(x|mm)'))[1]::numeric;
        v_notes := v_notes || format('Calibre %s is not in the calibre table; bore %s taken from the code.',
                                     v_cal, coalesce(v_bore::text || ' mm', 'unknown'));
      end if;
      if v_related is not null and cardinality(v_related) > 0 then
        v_notes := v_notes || format('%s is related to %s: compatible, with a warning (check the specification).',
                                     v_cal, array_to_string(v_related, ', '));
      end if;
      if v_label ilike '%check context%' then
        v_notes := v_notes || format('%s: %s.', v_cal, v_label);
      end if;
    end if;
  end if;

  v_smooth := v_id in ('firearms.shotguns', 'ammunition.shotgun') or coalesce(v_kind = 'shotgun', false);

  -- EU Military List by calibre (weapons: ML1 <= 12.7 mm, smooth-bore < 20 mm; otherwise ML2)
  if v_class = 'firearms' and v_bore is not null then
    if v_smooth then
      v_code := case when v_bore < 20 then 'ML1.b' else 'ML2.a' end;
      v_title := format('smooth-bore, %s mm %s 20 mm', v_bore, case when v_bore < 20 then '<' else '>=' end);
    else
      v_code := case when v_bore <= 12.7 then 'ML1.a' else 'ML2.a' end;
      v_title := format('bore %s mm %s 12.7 mm', v_bore, case when v_bore <= 12.7 then '<=' else '>' end);
    end if;
    v_ml := jsonb_build_object('code', v_code, 'basis', v_title,
                               'rule', 'Weapons: ML1 if calibre <= 12.7 mm (smooth-bore < 20 mm), otherwise ML2');
  elsif v_class = 'parts' and v_bore is not null and v_id <> 'parts.heavy_weapon_parts' then
    v_code := case
      when v_id in ('parts.accessories_tools', 'parts.sights_optics')
        then case when (v_smooth and v_bore < 20) or (not v_smooth and v_bore <= 12.7) then 'ML1.d' else 'ML2.c' end
      else case when (v_smooth and v_bore < 20) or (not v_smooth and v_bore <= 12.7) then 'ML1' else 'ML2' end
    end;
    v_ml := jsonb_build_object('code', v_code,
                               'basis', format('for a %s mm weapon (%s)', v_bore,
                                               case when v_code like 'ML1%' then 'ML1 weapon' else 'ML2 weapon' end),
                               'rule', 'Specially designed components and accessories follow their weapon: ML1 <= 12.7 mm, otherwise ML2');
  elsif v_id = 'parts.heavy_weapon_parts' then
    v_ml := jsonb_build_object('code', 'ML2', 'basis', 'parts of ML2 weapons',
                               'rule', 'Specially designed components follow their weapon');
  end if;
  if v_ml is null and v_id in ('firearms.machine_guns', 'firearms.precision_rifles') then
    v_notes := v_notes || 'ML1.a up to 12.7 mm, ML2.a above: give the calibre to decide.'::text;
  end if;

  -- NATO FSC by bore
  if v_bore is not null then
    if v_class = 'firearms' then
      v_code := case when v_bore <= 30 then '1005' when v_bore < 75 then '1010' when v_bore <= 125 then '1015'
                     when v_bore <= 150 then '1020' when v_bore <= 200 then '1025' end;
      v_title := case v_code when '1005' then 'Guns, through 30 mm'
                             when '1010' then 'Guns, over 30 mm up to 75 mm'
                             when '1015' then 'Guns, 75 mm through 125 mm'
                             when '1020' then 'Guns, over 125 mm through 150 mm'
                             when '1025' then 'Guns, over 150 mm through 200 mm' end;
      if v_code is not null then
        v_fsc := jsonb_build_object('code', v_code, 'label', v_title, 'basis', format('bore %s mm', v_bore),
                                    'rule', 'Guns: 1005 <= 30 mm, 1010 > 30 to < 75, 1015 75-125, 1020 > 125-150, 1025 > 150-200');
      end if;
      if v_id = 'firearms.launchers' then
        v_notes := v_notes || 'Rocket launchers are FSC 1055 whatever the bore.'::text;
      end if;
    elsif v_id in ('ammunition.small_calibre', 'ammunition.shotgun', 'ammunition.training_blank',
                   'ammunition.medium_calibre', 'ammunition.grenade_40mm', 'ammunition.mortar',
                   'ammunition.artillery', 'ammunition.components') then
      v_code := case when v_bore <= 30 then '1305' when v_bore < 75 then '1310' when v_bore <= 125 then '1315'
                     else '1320' end;
      v_title := case v_code when '1305' then 'Ammunition, through 30 mm'
                             when '1310' then 'Ammunition, over 30 mm up to 75 mm'
                             when '1315' then 'Ammunition, 75 mm through 125 mm'
                             when '1320' then 'Ammunition, over 125 mm' end;
      if v_code is not null then
        v_fsc := jsonb_build_object('code', v_code, 'label', v_title, 'basis', format('bore %s mm', v_bore),
                                    'rule', 'Ammunition: 1305 <= 30 mm, 1310 > 30 to < 75, 1315 75-125, 1320 > 125');
      end if;
      if v_id = 'ammunition.components' then
        v_notes := v_notes || 'Fuzes and primers for 1310-1320 ammunition are FSC 1390.'::text;
      end if;
    end if;
  end if;

  -- ADR "small arms": weapons <= 19.1 mm; every shotgun cartridge counts
  if v_class = 'firearms'
     or v_id in ('ammunition.small_calibre', 'ammunition.shotgun', 'ammunition.training_blank',
                 'ammunition.less_lethal', 'ammunition.medium_calibre', 'ammunition.grenade_40mm',
                 'ammunition.mortar', 'ammunition.artillery') then
    if v_id = 'ammunition.shotgun' or v_kind = 'shotgun' then
      v_adr_sa := true;
    elsif v_bore is not null then
      v_adr_sa := v_bore <= 19.1;
    end if;
  end if;
  if v_adr_sa and v_class = 'ammunition' then
    if v_id = 'ammunition.training_blank' then
      v_adr := jsonb_build_object('un_number', 'UN0014', 'classification', '1.4S', 'transport_category', 4,
                                  'name', 'Cartridges for weapons, blank',
                                  'note', 'Blanks; marking or practice rounds with a projectile are usually UN0012 1.4S, some UN0362 1.4G');
    elsif v_id = 'ammunition.less_lethal' then
      v_adr := jsonb_build_object('un_number', 'UN0012', 'classification', '1.4S', 'transport_category', 4,
                                  'name', 'Cartridges for weapons, inert projectile / cartridges, small arms',
                                  'note', 'Tear-producing rounds are UN0301 1.4G instead');
    else
      v_adr := jsonb_build_object('un_number', 'UN0012', 'classification', '1.4S', 'transport_category', 4,
                                  'name', 'Cartridges for weapons, inert projectile / cartridges, small arms',
                                  'note', 'Typical entry for small-arms cartridges');
    end if;
    v_notes := v_notes
      || '1.4S is transport category 4: unlimited quantity under the ADR 1.1.3.6 exemption (no orange plates); the transport document, fire extinguishers and driver training still apply.'::text
      || 'The tested article decides the real ADR entry (ADR 2.2.1.1.4): a UN number declared by the supplier overrides this default.'::text;
  elsif v_adr_sa and v_class = 'firearms' then
    v_notes := v_notes || 'Weapons are not dangerous goods; their cartridges (<= 19.1 mm) ship as small-arms ammunition, typically UN0012 1.4S.'::text;
  elsif v_adr_sa = false and v_class = 'ammunition' then
    v_notes := v_notes || 'Above 19.1 mm: not small-arms ammunition under ADR; see the family ADR rows (the entry depends on the article).'::text;
  end if;

  -- regime notes
  if v_id = 'firearms.shotguns' then
    v_notes := v_notes || 'Hunting or sporting smooth-bore weapons that are neither military-designed nor fully automatic are excluded from ML1.'::text;
  end if;
  if v_id = 'ammunition.training_blank' then
    v_notes := v_notes || 'Crimped blanks and pierced dummies are not ML3 (ML3 Note 2).'::text;
  end if;
  if v_class = 'ammunition' and v_id not in ('ammunition.hand_grenade', 'ammunition.heavy_ordnance',
                                            'ammunition.pyrotechnics', 'ammunition.explosives_demolition') then
    v_notes := v_notes || 'Ammunition for ML1/ML2 weapons is ML3.a; signalling cartridges are not ML3.'::text;
  end if;
  if v_class in ('firearms', 'ammunition', 'parts', 'misc_weapons') then
    v_notes := v_notes || 'The Firearms Directive (EU) 2021/555 does not apply to armed forces, police or public authorities (Art. 2(2)); for those buyers its category is a product attribute, not a buyer check.'::text;
  end if;
  if v_class = 'ammunition' then
    v_notes := v_notes || 'Under the Firearms Directive, ammunition follows the category of its firearm (Art. 13(1)).'::text;
  elsif v_class = 'parts' then
    v_notes := v_notes || 'Under the Firearms Directive, essential components take the category of their firearm.'::text;
  end if;
  if v_has_ml or v_ml is not null then
    v_notes := v_notes || 'Items on the EU Military List need an intra-EU transfer licence (Directive 2009/43/EC) or an export licence (Common Position 2008/944/CFSP), whatever the buyer type (our inference).'::text;
  end if;
  if not v_in_focus then
    v_notes := v_notes || 'Out-of-scope family: no regulatory profile is kept for it.'::text;
  end if;

  return jsonb_build_object(
           'family_id', v_id,
           'family_name', v_name,
           'class_id', v_class,
           'calibre', v_cal)
         || v_schemes
         || jsonb_build_object(
           'derived', jsonb_build_object(
             'calibre', v_cal,
             'calibre_label', v_label,
             'calibre_known', case when v_cal is null then null else v_known end,
             'bore_mm', v_bore,
             'calibre_standard', v_standard,
             'related_codes', to_jsonb(coalesce(v_related, '{}'::text[])),
             'ml_by_calibre', v_ml,
             'fsc_by_bore', v_fsc,
             'adr_small_arms', v_adr_sa,
             'adr_typical', v_adr),
           'notes', to_jsonb(v_notes));
end;
$$;

comment on function public.ui_regulatory_profile(text, text) is
  'UI read API: regulatory crosswalk of a family (and class) by scheme, plus calibre-derived ML, FSC and ADR (KB 25 section 7).';

-- ---------------------------------------------------------------------------------------------
-- Grants (security invoker: RLS of the underlying tables applies; product_items is public-read
-- only for visibility = 'public').
-- ---------------------------------------------------------------------------------------------
grant select on public.ui_items, public.ui_supplier_families, public.ui_tender_families to anon, authenticated;
grant execute on function public.ui_match_tender(text, int) to anon, authenticated;
grant execute on function public.ui_regulatory_profile(text, text) to anon, authenticated;
