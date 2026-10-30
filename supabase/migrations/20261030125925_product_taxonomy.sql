-- Product taxonomy for the four focus CPV classes (knowledge_base/25-product-families.md).
-- Two levels: CPV class (level 1) -> product family (level 2), a fixed attribute schema per family,
-- crosswalks to other schemes (CPV, EU Military List, NATO FSC, HS, ADR, STANAG), classified items
-- for both sides of the market, attribute-level claims, and a human-approved proposal queue.
-- Source of truth for the taxonomy rows: data/families.yaml (seeded by seed_product_taxonomy.py).

-- ---------- taxonomy ----------

create table if not exists public.product_taxonomy_versions (
  version int primary key,
  released_at timestamptz not null default now(),
  source text not null,                    -- e.g. 'data/families.yaml'
  notes text
);

create table if not exists public.product_attribute_defs (
  attr_key text primary key,               -- 'calibre'
  label text not null,
  data_type text not null check (data_type in
    ('text', 'number', 'integer', 'boolean', 'enum', 'enum_list', 'text_list', 'calibre')),
  unit text,                               -- UN/ECE Rec 20 code for numbers, e.g. 'MMT' (mm), 'GRM' (g)
  enum_values text[],
  description text not null
);

create table if not exists public.product_families (
  id text primary key,                     -- 'ammunition.small_calibre'
  parent_id text references public.product_families (id),
  level smallint not null check (level in (1, 2)),
  name text not null,
  description text not null,
  includes text[] not null default '{}',
  excludes text[] not null default '{}',
  in_focus boolean not null default true,  -- false: stored and classified, not matched in the demo
  review_flag text,                        -- e.g. 'treaty_restricted' = always escalate to a human
  status text not null default 'active' check (status in ('active', 'proposed', 'deprecated')),
  sort_order int not null default 0,
  taxonomy_version int not null references public.product_taxonomy_versions (version),
  scheme_uri text generated always as ('urn:salespatriot:product-family:' || id) stored,
  check ((level = 1) = (parent_id is null))
);
create index if not exists product_families_parent on public.product_families (parent_id);

create table if not exists public.product_family_attributes (
  family_id text references public.product_families (id) on delete cascade,
  attr_key text references public.product_attribute_defs (attr_key),
  required boolean not null default false,
  is_match_key boolean not null default false,   -- hard filter in matching when both sides have it
  enum_values text[],                             -- narrows the global enum for this family
  sort_order int not null default 0,
  primary key (family_id, attr_key)
);

-- SKOS-style mappings to other schemes. For CPV: 'exact' = the code alone decides the family,
-- 'candidate' = one of several families, the text decides.
create table if not exists public.product_family_crosswalk (
  family_id text references public.product_families (id) on delete cascade,
  scheme text not null check (scheme in
    ('cpv', 'eu_ml', 'fsc', 'hs', 'fd_2021_555', 'adr', 'stanag', 'unspsc')),
  code text not null,
  label text,
  relation text not null check (relation in ('exact', 'broad', 'narrow', 'candidate', 'related')),
  verified boolean not null default false,
  source_url text,
  note text,
  primary key (family_id, scheme, code)
);
create index if not exists product_family_crosswalk_code on public.product_family_crosswalk (scheme, code);

create table if not exists public.product_family_keywords (
  family_id text references public.product_families (id) on delete cascade,
  pattern text not null,                   -- case-insensitive POSIX regex, any language
  weight real not null default 1,
  primary key (family_id, pattern)
);

create table if not exists public.product_calibres (
  code text primary key,                   -- canonical: '5.56x45', '9x19', '12ga', '155mm'
  label text not null,
  kind text not null check (kind in ('pistol', 'rifle', 'heavy_mg', 'shotgun', 'rimfire', 'medium',
    'grenade', 'mortar', 'artillery', 'tank', 'naval', 'less_lethal', 'signal')),
  bore_mm numeric,
  case_length_mm numeric,
  standard text,
  related_codes text[] not null default '{}',   -- '.223' ~ '5.56x45': compatible, with a warning
  default_family_id text references public.product_families (id)
);

create table if not exists public.product_value_aliases (
  attr_key text references public.product_attribute_defs (attr_key),
  alias text not null,                     -- normalised: lower case, x for ×, no spaces
  canonical text not null,
  primary key (attr_key, alias)
);

create table if not exists public.product_noise_rules (
  cpv_code text primary key,
  label text not null,
  rule text not null,
  evidence text
);

-- ---------- items: what is bought or offered, one row per TED lot / offer line / demand line ----------

create table if not exists public.product_items (
  id uuid primary key default gen_random_uuid(),
  side text not null check (side in ('supply', 'demand')),
  source_kind text not null check (source_kind in
    ('ted_lot', 'ted_notice', 'offer', 'demand', 'declared', 'upload', 'catalogue')),
  source_ref text not null,                -- 'ted:720852-2026/LOT-0001', 'catalogue:<supplier id>/<n>'
  publication_number text,                 -- TED notice, when the item comes from TED
  lot_id text,
  org_id uuid,                             -- canonical company once resolved
  org_ref text,                            -- raw name / identifier until resolved
  title text,
  source_text text not null,               -- what the extractor read (original language)
  cpv_codes text[] not null default '{}',
  class_id text references public.product_families (id),
  family_id text references public.product_families (id),
  attrs jsonb not null default '{}',       -- current value per attribute (derived from claims)
  extra jsonb not null default '{}',       -- keys outside the family schema
  quantity numeric,
  unit text,
  classification_method text check (classification_method in ('cpv', 'rules', 'llm', 'agent', 'manual')),
  confidence real,
  verification_status text not null default 'inferred' check (verification_status in
    ('declared', 'document_verified', 'independently_verified', 'peer_attested', 'inferred')),
  needs_review boolean not null default false,
  visibility text not null default 'public' check (visibility in ('public', 'parties')),
  text_hash text generated always as (md5(source_text)) stored,
  processed_hash text,                     -- text_hash at the last classification
  extractor_version text,
  taxonomy_version int references public.product_taxonomy_versions (version),
  observed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (source_kind, source_ref)
);
create index if not exists product_items_family on public.product_items (family_id, side);
create index if not exists product_items_class on public.product_items (class_id);
create index if not exists product_items_calibre on public.product_items ((attrs ->> 'calibre'));
create index if not exists product_items_attrs on public.product_items using gin (attrs jsonb_path_ops);
create index if not exists product_items_pending_idx on public.product_items (created_at)
  where processed_hash is null or processed_hash <> text_hash;

-- ---------- claims (shared, knowledge_base/10): every attribute value with its evidence ----------

create table if not exists public.claims (
  id uuid primary key default gen_random_uuid(),
  subject text not null,                   -- 'item:<uuid>', 'company:pl-krs:0000041811'
  predicate text not null,                 -- 'HAS_FAMILY', 'attr:calibre', 'HOLDS'
  object text not null,                    -- 'ammunition.small_calibre', '9x19'
  source_kind text,                        -- 'ted', 'platform', 'upload', 'agent', 'llm', 'rules'
  source_url text,
  observed_at timestamptz not null default now(),
  confidence numeric,
  verification_status text not null check (verification_status in
    ('declared', 'document_verified', 'independently_verified', 'peer_attested', 'inferred')),
  extractor_version text,
  taxonomy_version int,
  visibility text not null default 'public' check (visibility in ('public', 'parties')),
  created_at timestamptz not null default now()
);
create index if not exists claims_subject on public.claims (subject);
create index if not exists claims_predicate_object on public.claims (predicate, object);

-- ---------- proposal queue: observations are automatic, schema changes are human-approved ----------

create table if not exists public.product_taxonomy_proposals (
  id bigint generated always as identity primary key,
  kind text not null check (kind in ('new_family', 'new_attribute', 'new_enum_value', 'new_alias',
    'new_keyword', 'split_family', 'merge_families', 'move_family')),
  target_family_id text references public.product_families (id),
  payload jsonb not null,
  support_count int not null default 0,
  example_item_ids uuid[] not null default '{}',
  status text not null default 'open' check (status in ('open', 'approved', 'rejected', 'applied')),
  created_by text not null default 'pattern-job',
  created_at timestamptz not null default now(),
  decided_by text,
  decided_at timestamptz,
  decision_note text
);

-- ---------- functions ----------

-- Attributes of a family including those inherited from its class (level 1).
create or replace function public.product_family_attribute_list(p_family_id text)
returns table (attr_key text, label text, data_type text, unit text, enum_values text[],
               description text, required boolean, is_match_key boolean, sort_order int)
language sql stable
set search_path = ''
as $$
  with fam as (
    select f.id, f.parent_id from public.product_families f where f.id = p_family_id
  ),
  links as (
    select fa.*, case when fa.family_id = (select id from fam) then 0 else 1 end as depth
    from public.product_family_attributes fa
    where fa.family_id in ((select id from fam), (select parent_id from fam))
  ),
  best as (
    select distinct on (l.attr_key) l.* from links l order by l.attr_key, l.depth
  )
  select d.attr_key, d.label, d.data_type, d.unit,
         coalesce(b.enum_values, d.enum_values), d.description, b.required, b.is_match_key,
         b.sort_order + b.depth * 1000
  from best b join public.product_attribute_defs d using (attr_key)
  order by 9, 1;
$$;

-- JSON Schema for one family: what the LLM (or an agent tool call) must return. Unknown keys go to "extra".
create or replace function public.product_family_schema(p_family_id text)
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
    'required', coalesce((select jsonb_agg(a.attr_key) from a where a.required), '[]'::jsonb)
  );
$$;

-- Stage 1 of the classification cascade: what the CPV codes alone say.
create or replace function public.product_family_candidates_by_cpv(p_cpv text[])
returns table (family_id text, class_id text, relation text, cpv_code text, is_noise_code boolean)
language sql stable
set search_path = ''
as $$
  select c.family_id, coalesce(f.parent_id, f.id), c.relation, c.code,
         exists (select 1 from public.product_noise_rules n where n.cpv_code = c.code)
  from public.product_family_crosswalk c
  join public.product_families f on f.id = c.family_id
  where c.scheme = 'cpv' and c.code = any (p_cpv) and f.status = 'active'
  order by case c.relation when 'exact' then 0 when 'narrow' then 1 when 'candidate' then 2 else 3 end;
$$;

-- Supply items that can serve one demand item: same family, and every match-key attribute that both
-- sides state must agree. Missing values on the supply side are allowed but rank lower.
create or replace function public.product_match_supply(p_demand_item uuid, p_limit int default 50)
returns table (item_id uuid, org_id uuid, org_ref text, family_id text, attrs jsonb,
               verification_status text, key_matches int, key_unknown int)
language sql stable
set search_path = ''
as $$
  with d as (select * from public.product_items where id = p_demand_item and side = 'demand'),
  keys as (
    select k.attr_key from d, public.product_family_attribute_list(d.family_id) k
    where k.is_match_key and d.attrs ? k.attr_key
  )
  select s.id, s.org_id, s.org_ref, s.family_id, s.attrs, s.verification_status,
         (select count(*) from keys where s.attrs ->> keys.attr_key = (select attrs ->> keys.attr_key from d))::int,
         (select count(*) from keys where not s.attrs ? keys.attr_key)::int
  from d join public.product_items s on s.family_id = d.family_id and s.side = 'supply'
  where s.visibility = 'public'
    and not exists (
      select 1 from keys
      where s.attrs ? keys.attr_key and s.attrs ->> keys.attr_key <> (select attrs ->> keys.attr_key from d))
  order by 7 desc, 8 asc,
    case s.verification_status when 'independently_verified' then 0 when 'document_verified' then 1
      when 'peer_attested' then 2 when 'declared' then 3 else 4 end
  limit p_limit;
$$;

create or replace view public.product_family_tree with (security_invoker = on) as
select f.id, f.level, f.parent_id, p.name as class_name, f.name, f.description, f.in_focus,
       f.review_flag, f.status, f.sort_order,
       (select array_agg(fa.attr_key order by fa.sort_order) from public.product_family_attributes fa
        where fa.family_id = f.id) as own_attributes,
       (select array_agg(c.code order by c.code) from public.product_family_crosswalk c
        where c.family_id = f.id and c.scheme = 'cpv') as cpv_codes
from public.product_families f left join public.product_families p on p.id = f.parent_id;

create or replace view public.product_items_pending with (security_invoker = on) as
select * from public.product_items
where processed_hash is null or processed_hash <> text_hash
   or taxonomy_version is distinct from (select max(version) from public.product_taxonomy_versions);

-- ---------- access: public read of public data, writes only with the service role ----------

alter table public.product_taxonomy_versions enable row level security;
alter table public.product_attribute_defs enable row level security;
alter table public.product_families enable row level security;
alter table public.product_family_attributes enable row level security;
alter table public.product_family_crosswalk enable row level security;
alter table public.product_family_keywords enable row level security;
alter table public.product_calibres enable row level security;
alter table public.product_value_aliases enable row level security;
alter table public.product_noise_rules enable row level security;
alter table public.product_items enable row level security;
alter table public.claims enable row level security;
alter table public.product_taxonomy_proposals enable row level security;

do $$
declare t text;
begin
  foreach t in array array['product_taxonomy_versions', 'product_attribute_defs', 'product_families',
    'product_family_attributes', 'product_family_crosswalk', 'product_family_keywords',
    'product_calibres', 'product_value_aliases', 'product_noise_rules', 'product_taxonomy_proposals']
  loop
    execute format('drop policy if exists "public read" on public.%I', t);
    execute format('create policy "public read" on public.%I for select to anon, authenticated using (true)', t);
  end loop;
end $$;

drop policy if exists "public read" on public.product_items;
create policy "public read" on public.product_items for select to anon, authenticated
  using (visibility = 'public');
drop policy if exists "public read" on public.claims;
create policy "public read" on public.claims for select to anon, authenticated
  using (visibility = 'public');
