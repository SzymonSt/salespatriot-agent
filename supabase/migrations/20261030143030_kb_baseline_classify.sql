-- Read-only port of the ontology classifier rules-v1 (public.product_classify_apply, stages 0-2) for TED text.
-- Same inputs and scoring, but it returns the decision instead of writing product_items or claims.
-- Catalogue-only logic (supplier categories, spec keys) is left out: baseline items are TED procedures.
create or replace function public.kb_baseline_classify(p_text text, p_cpv text[])
returns jsonb
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_text     text := lower(coalesce(p_text, ''));
  v_cpv      text[] := coalesce(p_cpv, '{}');
  v_focus    text[];
  v_classes  text[];
  v_allowed  text[];
  v_kw       jsonb;
  v_bon_fam  text[] := '{}';
  v_bon_pts  numeric[] := '{}';
  v_cal_codes text[] := '{}';
  v_exact    text[];
  v_ranked   jsonb;
  v_scores   jsonb;
  v_top      text;
  v_top_s    numeric := 0;
  v_second_s numeric := 0;
  v_exact_s  numeric;
  v_rival_s  numeric;
  v_noise    boolean := false;
  v_incidental boolean := false;
  v_conflict boolean := false;
  v_foreign  boolean := false;
  v_family   text;
  v_class    text;
  v_method   text := 'rules';
  v_conf     real;
  v_outcome  text;
  v_calibre  text;
  v_target   text;
  v_val      text;
  r          record;
begin
  select coalesce(array_agg(distinct x), '{}') into v_focus
  from unnest(v_cpv) x where x like '353%';

  select coalesce(array_agg(distinct c), '{}') into v_classes
  from (select case left(x, 4) when '3531' then 'misc_weapons' when '3532' then 'firearms'
                               when '3533' then 'ammunition'   when '3534' then 'parts' end as c
        from unnest(v_cpv) x) s
  where c is not null;

  select coalesce(array_agg(f.id), '{}') into v_allowed
  from public.product_families f
  where f.level = 2 and f.status = 'active'
    and (cardinality(v_classes) = 0 or f.parent_id = any (v_classes) or f.parent_id = 'out_of_scope');

  select coalesce(jsonb_object_agg(z.fam, z.s), '{}'::jsonb) into v_kw
  from (
    select k.family_id as fam,
           least(sum(k.weight),
                 case when k.family_id = 'ammunition.small_calibre' and 'ammunition' = any (v_classes)
                      then 1 else 3 end)::numeric as s
    from public.product_family_keywords k
    where k.family_id = any (v_allowed)
      and v_text ~* (case when k.pattern ~ '^[0-9]' then '(?<![0-9.,])' || k.pattern
                          when char_length(k.pattern) <= 4 and k.pattern ~ '^[[:alpha:]]' then '\m' || k.pattern
                          else k.pattern end)
    group by k.family_id
  ) z;

  for r in
    select c.code, c.known, c.default_family_id, c.kind
    from public.product_classify_calibres(p_text) c
    order by (not c.known), c.rank
  loop
    continue when r.code = any (v_cal_codes);
    v_cal_codes := v_cal_codes || r.code;
    continue when not r.known or r.default_family_id is null;

    v_target := r.default_family_id;
    if r.code in ('120mm', '105mm') and v_text ~ '(tank|czołg|panzer|leopard|abrams|char de combat|carro de combate|tanc)' then
      v_target := 'ammunition.artillery';
    elsif r.code = '122mm' and v_text ~ '(rakiet|rocket|raket|roquette|cohete|grad)' then
      v_target := 'ammunition.heavy_ordnance';
    end if;

    foreach v_val in array array_remove(array[
        v_target,
        case when r.kind in ('pistol', 'rifle', 'rimfire', 'heavy_mg', 'shotgun')
              and v_kw ? 'ammunition.training_blank' then 'ammunition.training_blank' end,
        case when r.kind in ('shotgun', 'grenade', 'less_lethal')
              and v_kw ? 'ammunition.less_lethal' then 'ammunition.less_lethal' end], null)
    loop
      if not v_val = any (v_bon_fam) then
        v_bon_fam := v_bon_fam || v_val;
        v_bon_pts := v_bon_pts || 2::numeric;
      end if;
    end loop;

    if 'firearms' = any (v_classes) then
      v_target := case r.kind when 'heavy_mg' then 'firearms.machine_guns'
                              when 'shotgun' then 'firearms.shotguns'
                              when 'mortar' then 'firearms.mortars'
                              when 'artillery' then 'firearms.artillery_systems'
                              when 'grenade' then 'firearms.launchers'
                              when 'less_lethal' then 'firearms.launchers' end;
      if v_target is not null and not v_target = any (v_bon_fam) then
        v_bon_fam := v_bon_fam || v_target;
        v_bon_pts := v_bon_pts || 1::numeric;
      end if;
    end if;
  end loop;

  -- stage 0: noise
  if cardinality(v_focus) = 1
     and exists (select 1 from public.product_noise_rules n where n.cpv_code = v_focus[1])
     and v_kw ? 'out_of_scope.noise' then
    v_noise := true;
  end if;

  -- stage 1: exact CPV families
  select coalesce(array_agg(distinct c.family_id), '{}') into v_exact
  from public.product_family_candidates_by_cpv(v_cpv) c
  join public.product_families f on f.id = c.family_id
  where c.relation = 'exact' and f.level = 2 and c.family_id = any (v_allowed);

  v_incidental := exists (select 1 from unnest(v_cpv) x
                          where x not like '353%'
                            and not exists (select 1 from public.product_family_crosswalk w
                                            where w.scheme = 'cpv' and w.code = x));
  if cardinality(v_exact) = 1 then
    v_conflict := exists (select 1
                          from unnest(v_bon_fam, v_bon_pts) as b(fam, s)
                          join public.product_families f on f.id = b.fam
                          join public.product_families e on e.id = v_exact[1]
                          where b.s = 2 and b.fam = any (v_allowed) and b.fam <> v_exact[1]
                            and f.parent_id = e.parent_id);
  end if;

  -- stage 2: scores
  with cpv as (
    select c.family_id as fam,
           (max(case when c.relation <> 'exact' then 0
                     when v_conflict
                       or (v_incidental and not (v_kw ? c.family_id) and not (c.family_id = any (v_bon_fam))) then 1
                     else 3 end)
          + max(case when c.relation = 'candidate' then 1 else 0 end)
          + max(case when c.relation = 'related' then 1 else 0 end))::numeric as s
    from public.product_family_candidates_by_cpv(v_cpv) c
    group by c.family_id
  ),
  contrib as (
    select e.key as fam, e.value::numeric as s from jsonb_each_text(v_kw) e
    union all
    select fam, s from cpv
    union all
    select b.fam, b.s from unnest(v_bon_fam, v_bon_pts) as b(fam, s)
  ),
  tot as (
    select p.fam, sum(p.s) as s
    from contrib p
    where p.fam = any (v_allowed)
    group by p.fam
    having sum(p.s) > 0
  )
  select coalesce(jsonb_agg(jsonb_build_object('f', fam, 's', s) order by s desc, fam), '[]'::jsonb)
    into v_ranked
  from tot;

  v_top      := v_ranked -> 0 ->> 'f';
  v_top_s    := coalesce((v_ranked -> 0 ->> 's')::numeric, 0);
  v_second_s := coalesce((v_ranked -> 1 ->> 's')::numeric, 0);
  select coalesce(jsonb_object_agg(e ->> 'f', (e ->> 's')::numeric), '{}'::jsonb) into v_scores
  from (select e from jsonb_array_elements(v_ranked) with ordinality a(e, o) where o <= 5) z;

  if cardinality(v_exact) = 1 then
    select coalesce(max((e ->> 's')::numeric) filter (where e ->> 'f' = v_exact[1]), 0),
           coalesce(max((e ->> 's')::numeric) filter (where e ->> 'f' <> v_exact[1]), 0)
      into v_exact_s, v_rival_s
    from jsonb_array_elements(v_ranked) e;
  end if;

  -- stage 0b: a weapons CPV only further down the list of a notice about something else (TED items)
  if cardinality(v_focus) > 0
     and v_cpv[1] not like '353%'
     and not exists (select 1 from public.product_family_crosswalk w where w.scheme = 'cpv' and w.code = v_cpv[1])
     and not exists (select 1 from jsonb_object_keys(v_kw) k where k not like 'out_of_scope.%')
     and cardinality(v_bon_fam) = 0 then
    v_foreign := true;
  end if;

  -- decide
  if v_noise then
    v_family := 'out_of_scope.noise'; v_method := 'rules'; v_conf := 0.9;  v_outcome := 'noise';
  elsif v_foreign then
    select coalesce(
             (select e ->> 'f' from jsonb_array_elements(v_ranked) e
              where e ->> 'f' like 'out_of_scope.%' and (e ->> 's')::numeric >= 2
                and (e ->> 's')::numeric >= 1 + coalesce((select max((x ->> 's')::numeric) from jsonb_array_elements(v_ranked) x
                                                          where x ->> 'f' like 'out_of_scope.%' and x ->> 'f' <> e ->> 'f'), 0)
              limit 1),
             'out_of_scope.noise')
      into v_family;
    v_method := 'rules'; v_conf := 0.6; v_outcome := 'foreign';
  elsif cardinality(v_exact) = 1 and v_exact_s > v_rival_s and not v_conflict
        and not (v_incidental and not (v_kw ? v_exact[1]) and not (v_exact[1] = any (v_bon_fam))) then
    v_family := v_exact[1];           v_method := 'cpv';
    v_conf := case when cardinality(v_classes) <= 1 then 0.95 else 0.85 end;  v_outcome := 'cpv';
  elsif v_top_s >= 3 and v_top_s - v_second_s >= 2 then
    v_family := v_top;                v_method := 'rules'; v_conf := 0.8;  v_outcome := 'rules';
  else
    v_family := null;                 v_method := 'none';  v_outcome := 'undecided';
  end if;

  if v_family is not null then
    select f.parent_id into v_class from public.product_families f where f.id = v_family;
  else
    if cardinality(v_classes) = 1 then
      v_class := v_classes[1];
    elsif cardinality(v_classes) > 1 and v_top is not null then
      select f.parent_id into v_class from public.product_families f
      where f.id = v_top and f.parent_id = any (v_classes);
    end if;
    v_conf := case when v_class is not null then 0.3 else 0.1 end;
  end if;

  -- calibre only for families (or classes) that have the attribute; prefer one whose default family matches
  if cardinality(v_cal_codes) > 0
     and exists (select 1 from public.product_family_attribute_list(coalesce(v_family, v_class)) a
                 where a.attr_key = 'calibre') then
    select d.code into v_calibre
    from unnest(v_cal_codes) with ordinality as d(code, o)
    join public.product_calibres c on c.code = d.code
    where c.default_family_id = v_family
    order by d.o limit 1;
    v_calibre := coalesce(v_calibre, v_cal_codes[1]);
  end if;

  return jsonb_build_object(
    'class_id', v_class,
    'family_id', v_family,
    'calibre', v_calibre,
    'method', v_method,
    'confidence', v_conf,
    'outcome', v_outcome,
    'scores', v_scores,
    'detected_calibres', to_jsonb(v_cal_codes),
    'extractor', 'rules-v1 (read-only port, kb_baseline_classify)');
end
$$;
revoke all on function public.kb_baseline_classify(text, text[]) from public, anon, authenticated;
grant execute on function public.kb_baseline_classify(text, text[]) to service_role;
