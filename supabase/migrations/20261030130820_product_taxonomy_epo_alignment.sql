-- Follow-up to product_taxonomy, after the ePO research (knowledge_base/25-product-families.md):
-- family rules and evidence, cpvsuppl tags, units from the EU measurement-unit table, and a schema
-- mode so TED extraction never forces values that the text does not state.

alter table public.product_families add column if not exists rules text[] not null default '{}';
alter table public.product_families add column if not exists evidence text;

alter table public.product_family_crosswalk drop constraint if exists product_family_crosswalk_scheme_check;
alter table public.product_family_crosswalk add constraint product_family_crosswalk_scheme_check
  check (scheme in ('cpv', 'cpvsuppl', 'eu_ml', 'fsc', 'hs', 'fd_2021_555', 'adr', 'stanag', 'unspsc', 'wassenaar'));

-- Line unit: EU measurement-unit authority code (H87 piece = rounds, pieces, sets; KGM; MTR; LTR).
alter table public.product_items drop constraint if exists product_items_unit_check;
alter table public.product_items add constraint product_items_unit_check
  check (unit is null or unit in ('H87', 'KGM', 'MTR', 'LTR', 'GRM', 'TNE'));
-- Extra item classifications, e.g. cpvsuppl tags FB01 / FB02 (epo:hasItemClassification).
alter table public.product_items add column if not exists item_classifications text[] not null default '{}';

create table if not exists public.product_cpvsuppl_tags (
  code text primary key,                   -- 'FB02' -> http://data.europa.eu/cpv/cpvsuppl/FB02
  label text not null,
  rule text not null
);
alter table public.product_cpvsuppl_tags enable row level security;
drop policy if exists "public read" on public.product_cpvsuppl_tags;
create policy "public read" on public.product_cpvsuppl_tags for select to anon, authenticated using (true);

-- p_mode = 'extraction' (TED and uploads): nothing is required, so the model never invents values.
-- p_mode = 'listing' (agent-created offers and demands): required attributes are enforced.
drop function if exists public.product_family_schema(text);
create or replace function public.product_family_schema(p_family_id text, p_mode text default 'extraction')
returns jsonb
language sql stable
set search_path = ''
as $$
  with a as (select * from public.product_family_attribute_list(p_family_id))
  select jsonb_build_object(
    '$schema', 'https://json-schema.org/draft/2020-12/schema',
    'title', p_family_id,
    'type', 'object',
    'additionalProperties', false,
    'properties', coalesce((
      select jsonb_object_agg(a.attr_key,
        jsonb_strip_nulls(jsonb_build_object(
          'description', a.label || ': ' || a.description || coalesce(' Unit: ' || a.unit || '.', ''),
          'type', case a.data_type
                    when 'number' then 'number'
                    when 'integer' then 'integer'
                    when 'boolean' then 'boolean'
                    when 'enum_list' then 'array'
                    when 'text_list' then 'array'
                    else 'string' end,
          'enum', case when a.data_type = 'enum' then to_jsonb(a.enum_values) end,
          'items', case when a.data_type = 'enum_list' then jsonb_build_object('type', 'string', 'enum', to_jsonb(a.enum_values))
                        when a.data_type = 'text_list' then jsonb_build_object('type', 'string') end
        )))
      from a), '{}'::jsonb)
      || jsonb_build_object('extra', jsonb_build_object(
           'type', 'object',
           'description', 'Facts stated in the text that have no field above. Never invent values.')),
    'required', case when p_mode = 'listing'
                  then coalesce((select jsonb_agg(a.attr_key) from a where a.required), '[]'::jsonb)
                  else '[]'::jsonb end
  );
$$;

-- Canonical calibre for any spelling: alias table first, then the "AxB" / "N mm" patterns.
create or replace function public.product_normalise_calibre(p_text text)
returns text
language sql stable
set search_path = ''
as $$
  with n as (
    select replace(replace(replace(lower(coalesce(p_text, '')), ',', '.'), '×', 'x'), ' ', '') as t
  ),
  alias as (
    select va.canonical from public.product_value_aliases va, n
    where va.attr_key = 'calibre' and va.alias = n.t
  ),
  code as (
    select c.code from public.product_calibres c, n where lower(c.code) = n.t
  ),
  pair as (
    select (regexp_match(n.t, '([0-9]{1,3}(\.[0-9]{1,2})?)(mm)?x([0-9]{2,3})'))[1] || 'x' ||
           (regexp_match(n.t, '([0-9]{1,3}(\.[0-9]{1,2})?)(mm)?x([0-9]{2,3})'))[4] as v from n
  ),
  mm as (
    select (regexp_match(n.t, '^([0-9]{2,3})mm'))[1] || 'mm' as v from n
  )
  select coalesce(
    (select canonical from alias limit 1),
    (select code from code limit 1),
    (select c.code from public.product_calibres c, pair where pair.v is not null and c.code = pair.v),
    (select v from pair),
    (select c.code from public.product_calibres c, mm where mm.v is not null and c.code = mm.v),
    (select v from mm)
  );
$$;
