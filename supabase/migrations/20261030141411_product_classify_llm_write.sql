-- Stage 3 writer (KB 26 §3 'LLM contract', §4 'Writing results'): stores one model decision for an item.
-- Used by the LLM pass (classification_method 'llm', extractor_version 'llm-agent-v1') and by any later
-- model worker. Validates against the taxonomy, never changes it:
--   * p_family must be an active level-2 family (or null = 'none': item stays undecided, needs_review);
--   * attrs keep only keys of product_family_attribute_list(p_family); enum / enum_list values must be in
--     the family's (narrowed) enum; calibre is normalised with product_normalise_calibre; anything
--     rejected goes to extra.llm.unmapped;
--   * when the family has a calibre attribute, p_attrs has no 'calibre' key and the text states exactly one
--     calibre (extra.detected_calibres), that calibre is used;
--   * confidence is capped at 0.9, needs_review = confidence < 0.8;
--   * claims: one 'HAS_FAMILY' and one 'attr:<key>' per model value, source_kind 'llm', 'inferred';
--     the item's earlier model claims are replaced, a contradicted rules family claim is removed.
create or replace function public.product_classify_llm_write(
  p_id         uuid,
  p_family     text,
  p_confidence real,
  p_attrs      jsonb default '{}'::jsonb,
  p_evidence   text default null,
  p_extra      jsonb default null,
  p_version    text default 'llm-agent-v1')
returns text
language plpgsql
volatile
set search_path = ''
as $$
declare
  it        public.product_items%rowtype;
  v_tax     integer;
  v_class   text;
  v_conf    real := least(greatest(coalesce(p_confidence, 0), 0), 0.9);
  v_keys    text[] := '{}';
  v_attrs   jsonb := '{}'::jsonb;
  v_model   jsonb := '{}'::jsonb;
  v_bad     jsonb := '{}'::jsonb;
  v_enum    text[];
  v_type    text;
  v_val     jsonb;
  v_cal     text;
  v_tags    text[];
  v_url     text;
  v_extra   jsonb;
  r         record;
begin
  select * into it from public.product_items where id = p_id for update;
  if not found then
    return 'missing';
  end if;
  select max(version) into v_tax from public.product_taxonomy_versions;

  if p_family is not null then
    select f.parent_id into v_class from public.product_families f
    where f.id = p_family and f.level = 2 and f.status = 'active';
    if v_class is null then
      raise exception 'product_classify_llm_write: % is not an active level-2 family', p_family;
    end if;
  else
    v_class := it.class_id;
  end if;

  select coalesce(array_agg(a.attr_key), '{}') into v_keys
  from public.product_family_attribute_list(coalesce(p_family, v_class)) a;

  -- model attributes, validated
  for r in select e.key, e.value from jsonb_each(coalesce(p_attrs, '{}'::jsonb)) e loop
    continue when r.value is null or r.value = 'null'::jsonb;
    if not r.key = any (v_keys) then
      v_bad := v_bad || jsonb_build_object(r.key, r.value);
      continue;
    end if;
    select a.data_type, a.enum_values into v_type, v_enum
    from public.product_family_attribute_list(coalesce(p_family, v_class)) a where a.attr_key = r.key;
    v_val := r.value;
    if r.key = 'calibre' then
      v_cal := public.product_normalise_calibre(r.value #>> '{}');
      if v_cal is null then
        v_bad := v_bad || jsonb_build_object(r.key, r.value);
        continue;
      end if;
      v_val := to_jsonb(v_cal);
    elsif v_type = 'enum' and not ((r.value #>> '{}') = any (coalesce(v_enum, '{}'))) then
      v_bad := v_bad || jsonb_build_object(r.key, r.value);
      continue;
    elsif v_type = 'enum_list' then
      if jsonb_typeof(r.value) <> 'array' then
        v_val := jsonb_build_array(r.value);
      end if;
      if exists (select 1 from jsonb_array_elements_text(v_val) x where not x = any (coalesce(v_enum, '{}'))) then
        v_bad := v_bad || jsonb_build_object(r.key, r.value);
        continue;
      end if;
    elsif v_type = 'text_list' and jsonb_typeof(r.value) <> 'array' then
      v_val := jsonb_build_array(r.value);
    end if;
    v_model := v_model || jsonb_build_object(r.key, v_val);
  end loop;

  -- one stated calibre fills the calibre when the model gave none
  if 'calibre' = any (v_keys) and not (coalesce(p_attrs, '{}'::jsonb) ? 'calibre')
     and jsonb_typeof(it.extra -> 'detected_calibres') = 'array'
     and jsonb_array_length(it.extra -> 'detected_calibres') = 1 then
    v_model := v_model || jsonb_build_object('calibre', it.extra -> 'detected_calibres' -> 0);
  end if;

  -- current values: earlier rules values that are still valid, then the model's
  select coalesce(jsonb_object_agg(e.key, e.value), '{}'::jsonb) into v_attrs
  from jsonb_each(it.attrs) e where e.key = any (v_keys);
  v_attrs := v_attrs || v_model;

  select coalesce(array_agg(distinct t order by t), '{}') into v_tags
  from unnest(
         array_remove(array[
           case when v_class in ('ammunition', 'firearms', 'parts', 'misc_weapons') then 'FB02' end,
           case when p_family in ('parts.small_arms_parts', 'parts.heavy_weapon_parts') then 'DA44' end,
           case when v_attrs -> 'end_use' ? 'military' then 'FB01' end,
           case when v_attrs -> 'end_use' ? 'law_enforcement' then 'FB03' end,
           case when (v_attrs ->> 'standards') ~* '(stanag|mil-std)' then 'FB13' end,
           case v_attrs ->> 'case_material' when 'brass' then 'AA44' when 'steel' then 'AA11' when 'polymer' then 'AB06' end
         ], null)
         || coalesce(array(select x from unnest(it.item_classifications) x
                           where x not in (select code from public.product_cpvsuppl_tags)), '{}')
       ) t;

  v_extra := coalesce(it.extra, '{}'::jsonb) - 'llm'
             || jsonb_build_object('llm', jsonb_strip_nulls(jsonb_build_object(
                  'family', coalesce(p_family, 'none'),
                  'evidence', p_evidence,
                  'unmapped', case when v_bad = '{}'::jsonb then null else v_bad end,
                  'version', p_version,
                  'at', now()) || coalesce(p_extra, '{}'::jsonb)));

  update public.product_items set
    class_id              = v_class,
    family_id             = p_family,
    attrs                 = v_attrs,
    extra                 = v_extra,
    classification_method = 'llm',
    confidence            = case when p_family is null then least(v_conf, 0.4) else v_conf end,
    needs_review          = p_family is null or v_conf < 0.8,
    processed_hash        = text_hash,
    taxonomy_version      = v_tax,
    extractor_version     = p_version,
    item_classifications  = v_tags,
    updated_at            = now()
  where id = p_id;

  -- claims
  delete from public.claims c
  where c.subject = 'item:' || p_id::text
    and (c.source_kind = 'llm'
         or (c.source_kind = 'rules' and c.predicate = 'HAS_FAMILY' and c.object is distinct from p_family)
         or (c.source_kind = 'rules' and c.predicate like 'attr:%' and not (substr(c.predicate, 6) = any (v_keys))));

  if it.publication_number is not null then
    select n.ted_url into v_url from public.ted_notices n where n.publication_number = it.publication_number limit 1;
    v_url := coalesce(v_url, 'https://ted.europa.eu/en/notice/-/detail/' || it.publication_number);
  else
    v_url := it.source_ref;
  end if;

  if p_family is not null then
    insert into public.claims (subject, predicate, object, source_kind, source_url, observed_at, confidence,
                               verification_status, extractor_version, taxonomy_version, visibility)
    values ('item:' || p_id::text, 'HAS_FAMILY', p_family, 'llm', v_url, coalesce(it.observed_at, now()), v_conf,
            'inferred', p_version, v_tax, coalesce(it.visibility, 'public'));
  end if;

  insert into public.claims (subject, predicate, object, source_kind, source_url, observed_at, confidence,
                             verification_status, extractor_version, taxonomy_version, visibility)
  select 'item:' || p_id::text, 'attr:' || e.key,
         case jsonb_typeof(e.value) when 'string' then e.value #>> '{}' else e.value::text end,
         'llm', v_url, coalesce(it.observed_at, now()), v_conf, 'inferred', p_version, v_tax,
         coalesce(it.visibility, 'public')
  from jsonb_each(v_model) e;

  return coalesce(p_family, 'none');
end
$$;

revoke execute on function public.product_classify_llm_write(uuid, text, real, jsonb, text, jsonb, text)
  from public, anon, authenticated;
