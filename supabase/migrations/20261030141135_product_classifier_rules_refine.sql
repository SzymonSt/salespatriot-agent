-- Rules classifier v1 refinements (still extractor_version 'rules-v1'; replaces product_classify_apply).
--  * Keyword stems that start with a digit ('30 ?mm', '20 ?mm') must not follow a digit, so that
--    '130mm' does not count as 30 mm and '120 mm' not as 20 mm.
--  * Stage 0b 'foreign': a TED item whose main (first) CPV is outside weapons and the crosswalk, and
--    whose text has no stem or calibre for any in-focus family (the 353 code is a stray one further
--    down the CPV list), goes to the best out_of_scope family
--    (score >= 2 and ahead of the other out_of_scope families), else out_of_scope.noise;
--    confidence 0.6, needs_review (e.g. 'Reagent strips', 'Water mains' notices with a stray 3533 code).
--  * needs_review follows the KB 26 thresholds for decided items (confidence < 0.8).
-- Items already classified by rules are re-queued at the end so the queue applies the new rules.

-- 2. Classify one item and write family, attrs, tags and claims. Returns the outcome:
--    'noise' | 'foreign' | 'cpv' | 'rules' | 'undecided' | 'kept' | 'missing'.
create or replace function public.product_classify_apply(p_id uuid)
returns text
language plpgsql
volatile
set search_path = ''
as $$
declare
  c_version  constant text := 'rules-v1';
  c_unknown  constant text := '(^|[^[:alpha:]])(not (stated|named|known|public|disclosed|specified|identified|retrieved|given)|unknown|unconfirmed|unclear|n/a)([^[:alpha:]]|$)|^[[:space:]]*(none|tbd|-)[[:space:]]*$';
  it         public.product_items%rowtype;
  v_tax      integer;
  v_text     text;
  v_cpv      text[];
  v_focus    text[];
  v_classes  text[];
  v_cat      text;
  v_cat_item jsonb;
  v_specs    jsonb := '{}'::jsonb;
  v_spec_cal text := '';
  v_hint_fam text[] := '{}';
  v_hint_pts numeric[] := '{}';
  v_bon_fam  text[] := '{}';
  v_bon_pts  numeric[] := '{}';
  v_cal_codes text[] := '{}';
  v_allowed  text[];
  v_exact    text[];
  v_ranked   jsonb;
  v_kw       jsonb;
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
  v_review   boolean;
  v_outcome  text;
  v_keys     text[];
  v_attrs    jsonb := '{}'::jsonb;
  v_unmapped jsonb := '{}'::jsonb;
  v_extra    jsonb;
  v_calibre  text;
  v_tags     text[];
  v_status   text;
  v_url      text;
  v_target   text;
  v_val      text;
  r          record;
begin
  select * into it from public.product_items where id = p_id for update;
  if not found then
    return 'missing';
  end if;

  select max(version) into v_tax from public.product_taxonomy_versions;

  -- a model, agent or person decided on this exact text: keep it, only stamp the taxonomy version
  if it.processed_hash is not null and it.processed_hash = it.text_hash
     and it.classification_method in ('llm', 'agent', 'manual') and it.family_id is not null then
    update public.product_items set taxonomy_version = v_tax where id = p_id;
    return 'kept';
  end if;

  v_text := lower(coalesce(it.source_text, ''));
  v_cpv  := coalesce(it.cpv_codes, '{}');

  select coalesce(array_agg(distinct x), '{}') into v_focus
  from unnest(v_cpv) x where x like '353%';

  select coalesce(array_agg(distinct c), '{}') into v_classes
  from (select case left(x, 4) when '3531' then 'misc_weapons' when '3532' then 'firearms'
                               when '3533' then 'ammunition'   when '3534' then 'parts' end as c
        from unnest(v_cpv) x) s
  where c is not null;

  -- catalogue items: the supplier's category is a strong hint; specs map to the dictionary
  if it.source_kind = 'catalogue' or it.source_table = 'suppliers.catalogue' then
    if it.supplier_id is not null then
      select s.catalogue -> 'items' -> coalesce(it.source_index, (regexp_match(it.source_pk, '/([0-9]+)$'))[1]::integer)
        into v_cat_item
      from public.suppliers s where s.id = it.supplier_id;
    end if;
    v_cat := coalesce(it.extra -> 'catalogue' ->> 'category', v_cat_item ->> 'category');
    if jsonb_typeof(v_cat_item -> 'specs') = 'object' then
      v_specs := v_cat_item -> 'specs';
    end if;

    case v_cat
      when 'Ammunition' then v_classes := v_classes || 'ammunition'::text;
      when 'Firearms' then v_classes := v_classes || 'firearms'::text;
      when 'Parts of firearms and ammunition' then v_classes := v_classes || 'parts'::text;
      when 'Miscellaneous weapons' then v_classes := v_classes || 'misc_weapons'::text;
      when 'Optics and sights' then
        v_hint_fam := array['parts.sights_optics'];               v_hint_pts := array[3];
      when 'Protective equipment' then
        v_hint_fam := array['out_of_scope.protective_equipment']; v_hint_pts := array[3];
      when 'Services' then
        v_hint_fam := array['out_of_scope.services'];             v_hint_pts := array[3];
      when 'Explosives and energetics' then
        v_classes  := v_classes || 'ammunition'::text;
        v_hint_fam := array['ammunition.explosives_demolition', 'ammunition.components', 'ammunition.pyrotechnics'];
        v_hint_pts := array[1, 1, 1];
      when 'Less-lethal and police equipment' then
        v_hint_fam := array['ammunition.less_lethal', 'misc_weapons.other_weapons', 'out_of_scope.law_enforcement_equipment'];
        v_hint_pts := array[1, 1, 1];
      else
        null;
    end case;

    -- calibre-bearing spec values are read first
    select coalesce(string_agg(
             case when lower(e.key) = 'gauge' and (e.value #>> '{}') ~ '^[[:space:]]*(12|16|20)[[:space:]]*$'
                  then btrim(e.value #>> '{}') || ' ga'
                  else e.value #>> '{}' end, ' ; '), '')
      into v_spec_cal
    from jsonb_each(v_specs) e
    where lower(e.key) in ('calibre', 'calibres', 'calibre_of_parent_weapon', 'standard_calibre', 'platform_calibre', 'gauge');
  end if;

  select coalesce(array_agg(distinct x), '{}') into v_classes from unnest(v_classes) x;

  -- catalogue items are scored on product + brand_model + spec values (spec key names such as
  -- 'scope', 'link' or 'magazine' must not hit keyword stems)
  if v_cat_item is not null then
    v_text := lower(concat_ws(' ', v_cat_item ->> 'product', v_cat_item ->> 'brand_model',
                              (select string_agg(case jsonb_typeof(e.value) when 'string' then e.value #>> '{}'
                                                      else e.value::text end, ' ')
                               from jsonb_each(v_specs) e)));
  end if;

  -- candidate families: the item's classes plus out_of_scope; all families when no class is known
  select coalesce(array_agg(f.id), '{}') into v_allowed
  from public.product_families f
  where f.level = 2 and f.status = 'active'
    and (cardinality(v_classes) = 0 or f.parent_id = any (v_classes) or f.parent_id = 'out_of_scope');

  -- keyword stems: +1 per stem, max 3 per family. The small_calibre stems are generic ammunition
  -- words (naboj, patron, cartridge, ammunition ...): once the class is known they count once.
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

  -- calibres: spec values first, then the text. A known calibre gives +2 to its default family,
  -- and +2 to training_blank / less_lethal when their own stems hit (blank, marking, rubber ...).
  for r in
    select d.code, d.known, d.default_family_id, d.kind
    from (select c.*, 0 as src from public.product_classify_calibres(v_spec_cal) c
          union all
          select c.*, 1 as src from public.product_classify_calibres(it.source_text) c) d
    order by d.src, (not d.known), d.rank
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

    -- weapon notices: a calibre kind points at the matching weapon family (+1)
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
  v_val := null;

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

  -- an exact CPV is weak (+1 instead of +3, and cannot decide alone) when
  --  * it looks incidental: the notice also has CPVs outside weapons and the crosswalk, and the
  --    text says nothing for that family (e.g. 35322400 'Mortars' on a building-materials notice), or
  --  * a stated calibre points at another family of the same class (82 mm under 35331400 'Bombs')
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
    union all
    select h.fam, h.s from unnest(v_hint_fam, v_hint_pts) as h(fam, s)
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

  -- stage 0b: the notice is about something else. Its main (first) CPV is outside weapons and the
  -- crosswalk, a weapons CPV only appears further down the list, and the text has no stem or
  -- calibre for any in-focus family -> the best out_of_scope family, else out_of_scope.noise
  -- (confidence 0.6, needs_review). TED items only.
  if it.source_kind in ('ted_lot', 'ted_notice') and cardinality(v_focus) > 0
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
    v_family := null;                 v_method := 'rules'; v_outcome := 'undecided';
  end if;

  if v_family is not null then
    select f.parent_id into v_class from public.product_families f where f.id = v_family;
    v_review := v_conf < 0.8;
  else
    if cardinality(v_classes) = 1 then
      v_class := v_classes[1];
    elsif cardinality(v_classes) > 1 and v_top is not null then
      select f.parent_id into v_class from public.product_families f
      where f.id = v_top and f.parent_id = any (v_classes);
    end if;
    v_conf   := case when v_class is not null then 0.3 else 0.1 end;
    v_review := true;
  end if;

  -- an earlier model decision on the same text survives an undecided rules pass
  if v_family is null and it.processed_hash is not null and it.processed_hash = it.text_hash
     and it.classification_method in ('llm', 'agent', 'manual') and it.family_id is not null then
    update public.product_items set taxonomy_version = v_tax where id = p_id;
    return 'kept';
  end if;

  -- attributes: only keys of the family (or class) attribute list
  select coalesce(array_agg(a.attr_key), '{}') into v_keys
  from public.product_family_attribute_list(coalesce(v_family, v_class)) a;

  if v_cat_item is not null then
    v_val := btrim(regexp_replace(coalesce(v_cat_item ->> 'brand_model', ''), '[[:space:]]*[(\[].*$', ''));
    if v_val <> '' and v_val !~* c_unknown and 'brand_model' = any (v_keys) then
      v_attrs := v_attrs || jsonb_build_object('brand_model', left(v_val, 200));
    end if;
  end if;

  if 'calibre' = any (v_keys) and cardinality(v_cal_codes) > 0 then
    select d.code into v_calibre
    from unnest(v_cal_codes) with ordinality as d(code, o)
    join public.product_calibres c on c.code = d.code
    where c.default_family_id = v_family
    order by d.o limit 1;
    v_calibre := coalesce(v_calibre, v_cal_codes[1]);
    v_attrs := v_attrs || jsonb_build_object('calibre', v_calibre);
  end if;

  for r in select e.key, e.value from jsonb_each(v_specs) e loop
    v_val := case jsonb_typeof(r.value)
               when 'string' then r.value #>> '{}'
               when 'array' then (select string_agg(x #>> '{}', ', ') from jsonb_array_elements(r.value) x)
               else r.value::text end;
    v_target := case lower(r.key)
      when 'calibre' then 'calibre' when 'calibres' then 'calibre' when 'calibre_of_parent_weapon' then 'calibre'
      when 'standard_calibre' then 'calibre' when 'platform_calibre' then 'calibre' when 'gauge' then 'calibre'
      when 'brand' then 'brand_model' when 'oem' then 'brand_model' when 'manufacturer' then 'brand_model'
      when 'model' then 'brand_model'
      when 'magnification' then 'magnification'
      when 'packaging' then 'packaging' when 'packing' then 'packaging'
      when 'standard' then 'standards'
      when 'platform' then 'compatible_platforms' when 'platforms' then 'compatible_platforms'
      when 'compatible_weapons' then 'compatible_platforms'
      when 'link' then 'link_type'
      when 'nsn' then 'nsn' when 'part_number' then 'part_number'
      when 'country_of_origin' then 'country_of_origin'
      else null end;
    if v_target = 'calibre' and v_attrs ? 'calibre' then
      continue;  -- consumed by the calibre detection above
    end if;
    if v_target is null or v_target = 'calibre' or not v_target = any (v_keys)
       or v_val is null or btrim(v_val) = ''
       or v_val ~* c_unknown then
      v_unmapped := v_unmapped || jsonb_build_object(r.key, r.value);
    elsif v_target in ('compatible_platforms', 'standards') then
      v_attrs := jsonb_set(v_attrs, array[v_target],
                           coalesce(v_attrs -> v_target, '[]'::jsonb) || to_jsonb(left(v_val, 300)));
    elsif v_attrs ? v_target then
      v_unmapped := v_unmapped || jsonb_build_object(r.key, r.value);
    else
      v_attrs := v_attrs || jsonb_build_object(v_target, left(v_val, 300));
    end if;
  end loop;

  -- extra: keep the bridge's keys, replace the classifier's own
  v_extra := coalesce(it.extra, '{}'::jsonb) - 'detected_calibres' - 'unmapped_specs' - 'rules_scores';
  if cardinality(v_cal_codes) > 0 then
    v_extra := v_extra || jsonb_build_object('detected_calibres', to_jsonb(v_cal_codes));
  end if;
  if v_unmapped <> '{}'::jsonb then
    v_extra := v_extra || jsonb_build_object('unmapped_specs', v_unmapped);
  end if;
  if v_scores <> '{}'::jsonb then
    v_extra := v_extra || jsonb_build_object('rules_scores', v_scores);
  end if;

  -- cpvsuppl tags (product_cpvsuppl_tags); codes from other sources are kept
  select coalesce(array_agg(distinct t order by t), '{}') into v_tags
  from unnest(
         array_remove(array[
           case when v_class in ('ammunition', 'firearms', 'parts', 'misc_weapons') then 'FB02' end,
           case when v_family in ('parts.small_arms_parts', 'parts.heavy_weapon_parts') then 'DA44' end,
           case when v_attrs -> 'end_use' ? 'military' then 'FB01' end,
           case when v_attrs -> 'end_use' ? 'law_enforcement' then 'FB03' end,
           case when (v_attrs ->> 'standards') ~* '(stanag|mil-std)' then 'FB13' end,
           case v_attrs ->> 'case_material' when 'brass' then 'AA44' when 'steel' then 'AA11' when 'polymer' then 'AB06' end
         ], null)
         || coalesce(array(select x from unnest(it.item_classifications) x
                           where x not in (select code from public.product_cpvsuppl_tags)), '{}')
       ) t;

  update public.product_items set
    class_id              = v_class,
    family_id             = v_family,
    attrs                 = v_attrs,
    extra                 = v_extra,
    classification_method = v_method,
    confidence            = v_conf,
    needs_review          = v_review,
    processed_hash        = text_hash,
    taxonomy_version      = v_tax,
    extractor_version     = c_version,
    item_classifications  = v_tags,
    updated_at            = now()
  where id = p_id;

  -- claims: replace this item's machine claims (rules, and model claims made on an older text)
  delete from public.claims where subject = 'item:' || p_id::text and source_kind in ('rules', 'llm');

  v_status := case when it.source_kind in ('ted_lot', 'ted_notice') then 'document_verified' else 'declared' end;
  if it.publication_number is not null then
    select n.ted_url into v_url from public.ted_notices n where n.publication_number = it.publication_number limit 1;
    v_url := coalesce(v_url, 'https://ted.europa.eu/en/notice/-/detail/' || it.publication_number);
  else
    v_url := it.source_ref;
  end if;

  if v_family is not null then
    insert into public.claims (subject, predicate, object, source_kind, source_url, observed_at, confidence,
                               verification_status, extractor_version, taxonomy_version, visibility)
    values ('item:' || p_id::text, 'HAS_FAMILY', v_family, 'rules', v_url, coalesce(it.observed_at, now()), v_conf,
            v_status, c_version, v_tax, coalesce(it.visibility, 'public'));
  end if;

  insert into public.claims (subject, predicate, object, source_kind, source_url, observed_at, confidence,
                             verification_status, extractor_version, taxonomy_version, visibility)
  select 'item:' || p_id::text, 'attr:' || e.key,
         case jsonb_typeof(e.value) when 'string' then e.value #>> '{}' else e.value::text end,
         'rules', v_url, coalesce(it.observed_at, now()), 0.9, v_status, c_version, v_tax, coalesce(it.visibility, 'public')
  from jsonb_each(v_attrs) e;

  return v_outcome;
end
$$;

-- re-queue the classifier's own rules decisions (model / agent / manual decisions are untouched)
update public.product_items
   set processed_hash = null
 where extractor_version = 'rules-v1' and classification_method in ('rules', 'cpv');
