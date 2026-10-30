-- Product bridge: feeds public.product_items automatically from the company-profile and TED tables
-- (supplier_contracts, suppliers.catalogue, ted_notices, ted_awards), following the "Source -> item mapping"
-- of the merge contract (knowledge_base/27-ontology-integration-guide.md).
--
-- Rules:
--   * Upsert by (source_kind, source_ref). source_text is only rewritten when it differs, so text_hash
--     (generated md5) stays the same and the classifier's work (processed_hash = text_hash) is kept.
--   * Triggers NEVER make the source write fail: every body is wrapped in an exception block that raises a warning.
--   * A DELETE in a source table does NOT delete items (supabase_sync.py deletes and re-inserts supplier_contracts
--     on every run). Orphans are removed by product_bridge_cleanup_orphans() (hourly pg_cron job), once their
--     source row is gone and source_seen_at is older than 1 hour.
--   * Ontology/taxonomy rows are not touched.

-- ---------------------------------------------------------------------------------------------------------
-- 1. Helpers
-- ---------------------------------------------------------------------------------------------------------

-- Digits of an identifier ('DE 811144119' -> '811144119'); null when fewer than 5 digits ('N/A', '', null).
create or replace function public.product_bridge_digits(p_value text)
returns text
language sql
immutable
set search_path = ''
as $$
  select case when length(x.d) >= 5 then x.d end
  from (select regexp_replace(coalesce(p_value, ''), '\D', '', 'g') as d) x
$$;

-- Normalised organisation name for equality matching: lower, trimmed, single spaces; null when empty.
create or replace function public.product_bridge_norm_name(p_value text)
returns text
language sql
immutable
set search_path = ''
as $$
  select nullif(lower(btrim(regexp_replace(coalesce(p_value, ''), '\s+', ' ', 'g'))), '')
$$;

-- 'YYYY-MM-DD...' -> date, anything else (or an invalid date) -> null.
create or replace function public.product_bridge_try_date(p_value text)
returns date
language plpgsql
immutable
set search_path = ''
as $$
begin
  if p_value is null or p_value !~ '^\d{4}-\d{2}-\d{2}' then
    return null;
  end if;
  return left(p_value, 10)::date;
exception when others then
  return null;
end
$$;

-- Joins text parts with newlines, skipping empty parts and case-insensitive duplicates (order kept).
create or replace function public.product_bridge_join_text(variadic p_parts text[])
returns text
language plpgsql
immutable
set search_path = ''
as $$
declare
  v_out  text[] := '{}';
  v_seen text[] := '{}';
  v_part text;
begin
  foreach v_part in array coalesce(p_parts, '{}'::text[]) loop
    v_part := btrim(coalesce(v_part, ''), E' \t\r\n');
    continue when v_part = '';
    continue when lower(v_part) = any (v_seen);
    v_seen := v_seen || lower(v_part);
    v_out  := v_out || v_part;
  end loop;
  return nullif(array_to_string(v_out, E'\n'), '');
end
$$;

-- Stable ref of one catalogue item (contract: catalogue:<supplier_id>/<md5(lower(product)|brand_model|cpv)>).
create or replace function public.product_bridge_catalogue_ref(p_supplier_id text, p_item jsonb)
returns text
language sql
immutable
set search_path = ''
as $$
  select 'catalogue:' || p_supplier_id || '/' ||
         md5(lower(coalesce(p_item->>'product', '')) || '|' ||
             coalesce(p_item->>'brand_model', '') || '|' ||
             coalesce(p_item->>'cpv', ''))
$$;

-- Text of one catalogue item: product, brand_model, then specs as "key: value" lines (sorted by key).
create or replace function public.product_bridge_catalogue_text(p_item jsonb)
returns text
language sql
immutable
set search_path = ''
as $$
  select public.product_bridge_join_text(
    p_item->>'product',
    p_item->>'brand_model',
    (select string_agg(e.key || ': ' || btrim(case when jsonb_typeof(e.value) = 'string' then e.value #>> '{}'
                                                   else e.value::text end), E'\n' order by e.key)
       from jsonb_each(case when jsonb_typeof(p_item->'specs') = 'object' then p_item->'specs'
                            else '{}'::jsonb end) e
      where jsonb_typeof(e.value) <> 'null'
        and btrim(case when jsonb_typeof(e.value) = 'string' then e.value #>> '{}' else e.value::text end) <> '')
  )
$$;

-- Supplier for a TED winner: digits-only identifier match first, else legal_name/name equality.
-- Guard: when both countries are known they must be equal (avoids generic-name and short-number collisions).
create or replace function public.product_bridge_match_supplier(p_identifier text, p_name text, p_country text)
returns text
language sql
stable
set search_path = ''
as $$
  select s.id
  from public.suppliers s
  where (s.country is null or p_country is null or s.country = p_country)
    and (
      (public.product_bridge_digits(p_identifier) is not null
       and public.product_bridge_digits(s.identifier) = public.product_bridge_digits(p_identifier))
      or
      (public.product_bridge_norm_name(p_name) is not null
       and public.product_bridge_norm_name(p_name) in (public.product_bridge_norm_name(s.legal_name),
                                                        public.product_bridge_norm_name(s.name)))
    )
  order by coalesce(public.product_bridge_digits(s.identifier) = public.product_bridge_digits(p_identifier), false) desc,
           s.id
  limit 1
$$;

-- Buyer for a TED notice: same rules against public.buyers.
create or replace function public.product_bridge_match_buyer(p_identifier text, p_name text, p_country text)
returns text
language sql
stable
set search_path = ''
as $$
  select b.id
  from public.buyers b
  where (b.country is null or p_country is null or b.country = p_country)
    and (
      (public.product_bridge_digits(p_identifier) is not null
       and public.product_bridge_digits(b.identifier) = public.product_bridge_digits(p_identifier))
      or
      (public.product_bridge_norm_name(p_name) is not null
       and public.product_bridge_norm_name(p_name) in (public.product_bridge_norm_name(b.legal_name),
                                                        public.product_bridge_norm_name(b.name)))
    )
  order by coalesce(public.product_bridge_digits(b.identifier) = public.product_bridge_digits(p_identifier), false) desc,
           b.id
  limit 1
$$;

-- ---------------------------------------------------------------------------------------------------------
-- 2. Generic upsert into product_items
-- ---------------------------------------------------------------------------------------------------------

create or replace function public.product_bridge_upsert_item(
  p_side                text,
  p_source_kind         text,
  p_source_ref          text,
  p_source_table        text,
  p_source_pk           text,
  p_source_index        integer,
  p_supplier_id         text,
  p_buyer_id            text,
  p_publication_number  text,
  p_lot_id              text,
  p_org_ref             text,
  p_title               text,
  p_source_text         text,
  p_cpv_codes           text[],
  p_observed_at         timestamptz,
  p_verification_status text,
  p_extra               jsonb
)
returns uuid
language plpgsql
set search_path = ''
as $$
declare
  v_id uuid;
begin
  insert into public.product_items as i (
    side, source_kind, source_ref, source_table, source_pk, source_index,
    supplier_id, buyer_id, publication_number, lot_id, org_ref, title, source_text, cpv_codes,
    observed_at, verification_status, extra, source_seen_at, updated_at)
  values (
    p_side, p_source_kind, p_source_ref, p_source_table, p_source_pk, p_source_index,
    p_supplier_id, p_buyer_id, p_publication_number, p_lot_id, p_org_ref, p_title, p_source_text,
    coalesce(p_cpv_codes, '{}'::text[]),
    p_observed_at, coalesce(p_verification_status, 'inferred'), coalesce(p_extra, '{}'::jsonb), now(), now())
  on conflict (source_kind, source_ref) do update set
    side                = excluded.side,
    source_table        = excluded.source_table,
    source_pk           = excluded.source_pk,
    source_index        = excluded.source_index,
    supplier_id         = excluded.supplier_id,
    buyer_id            = excluded.buyer_id,
    publication_number  = excluded.publication_number,
    lot_id              = excluded.lot_id,
    org_ref             = excluded.org_ref,
    title               = excluded.title,
    -- only rewrite the text when it changed: text_hash (generated) then stays and classification is kept
    source_text         = case when i.source_text is distinct from excluded.source_text
                               then excluded.source_text else i.source_text end,
    cpv_codes           = excluded.cpv_codes,
    observed_at         = excluded.observed_at,
    verification_status = excluded.verification_status,
    -- shallow merge: the bridge owns its top-level key ('contract'|'catalogue'|'notice'|'award'), others are kept
    extra               = i.extra || excluded.extra,
    source_seen_at      = now(),
    updated_at          = now()
  returning i.id into v_id;
  return v_id;
end
$$;

-- ---------------------------------------------------------------------------------------------------------
-- 3. Per-source upserts
-- ---------------------------------------------------------------------------------------------------------

-- supplier_contracts row -> ted_lot / supply, ref contract:<supplier_id>/<notice_number>/<lot_id or 'notice'>
create or replace function public.product_bridge_upsert_contract(p_id bigint)
returns uuid
language plpgsql
set search_path = ''
as $$
declare
  c        public.supplier_contracts%rowtype;
  v_sid    text;
  v_name   text;
  v_lot    text;
  v_tail   text;
begin
  select * into c from public.supplier_contracts where id = p_id;
  if not found or c.supplier_id is null or nullif(btrim(c.notice_number), '') is null then
    return null;
  end if;
  select s.id, s.name into v_sid, v_name from public.suppliers s where s.id = c.supplier_id;
  v_lot  := nullif(btrim(c.lot_id), '');
  v_tail := c.supplier_id || '/' || c.notice_number || '/' || coalesce(v_lot, 'notice');

  return public.product_bridge_upsert_item(
    'supply', 'ted_lot', 'contract:' || v_tail, 'supplier_contracts', v_tail, null,
    v_sid, null, c.notice_number, v_lot,
    coalesce(v_name, c.supplier_id),
    coalesce(nullif(btrim(c.title), ''), nullif(btrim(c.notice_title), ''), nullif(btrim(c.contract_title), '')),
    coalesce(public.product_bridge_join_text(c.title, c.description, c.contract_title, c.notice_title),
             'TED contract ' || c.notice_number),
    c.cpv_codes,
    coalesce(c.award_date, c.published)::timestamptz,
    'document_verified',
    jsonb_build_object('contract', jsonb_strip_nulls(jsonb_build_object(
      'buyer', c.buyer,
      'buyer_country', c.buyer_country,
      'notice_type', c.notice_type,
      'published', c.published,
      'award_date', c.award_date,
      'contract_signed', c.contract_signed,
      'quantity', c.quantity,
      'duration', c.duration,
      'framework_agreement', c.framework_agreement,
      'awarded_value', c.awarded_value,
      'awarded_currency', c.awarded_currency,
      'tenders_received', c.tenders_received,
      'winner_confirmed', c.winner_confirmed,
      'ted_url', c.ted_url))));
end
$$;

-- suppliers.catalogue->'items' -> catalogue / supply. Upserts every item; when items is an array, deletes this
-- supplier's catalogue items whose ref is no longer present. Returns the number of items upserted.
create or replace function public.product_bridge_sync_catalogue(p_supplier_id text)
returns integer
language plpgsql
set search_path = ''
as $$
declare
  v_sid    text;
  v_name   text;
  v_cat    jsonb;
  v_item   jsonb;
  v_idx    integer;
  v_ref    text;
  v_refs   text[] := '{}';
  v_vs     text;
  v_cpv    text;
  v_prefix text;
  v_n      integer := 0;
begin
  select s.id, s.name, s.catalogue into v_sid, v_name, v_cat from public.suppliers s where s.id = p_supplier_id;
  if not found then
    return 0;  -- supplier gone: items are left to product_bridge_cleanup_orphans()
  end if;

  for v_item, v_idx in
    select e.item, (e.ord - 1)::integer
    from jsonb_array_elements(case when jsonb_typeof(v_cat->'items') = 'array' then v_cat->'items'
                                   else '[]'::jsonb end) with ordinality e(item, ord)
  loop
    continue when jsonb_typeof(v_item) <> 'object' or nullif(btrim(v_item->>'product'), '') is null;
    v_ref  := public.product_bridge_catalogue_ref(v_sid, v_item);
    v_refs := v_refs || v_ref;
    v_vs   := v_item->'evidence'->>'verification_status';
    if v_vs is null or v_vs not in ('declared', 'document_verified', 'independently_verified', 'peer_attested', 'inferred') then
      v_vs := 'declared';
    end if;
    v_cpv  := nullif(btrim(v_item->>'cpv'), '');

    perform public.product_bridge_upsert_item(
      'supply', 'catalogue', v_ref, 'suppliers.catalogue', v_sid || '/' || v_idx, v_idx,
      v_sid, null, null, null,
      v_name,
      btrim(v_item->>'product'),
      public.product_bridge_catalogue_text(v_item),
      case when v_cpv is null then '{}'::text[] else array[v_cpv] end,
      public.product_bridge_try_date(v_item->'track_record'->>'last_awarded')::timestamptz,
      v_vs,
      jsonb_build_object('catalogue', jsonb_strip_nulls(jsonb_build_object(
        'category', v_item->'category',
        'confidence', v_item->'confidence',
        'commercial', v_item->'commercial',
        'unit', v_item->'unit',
        'evidence', v_item->'evidence',
        'track_record', v_item->'track_record'))));
    v_n := v_n + 1;
  end loop;

  if jsonb_typeof(v_cat->'items') = 'array' then
    v_prefix := 'catalogue:' || v_sid || '/';
    delete from public.product_items i
     where i.source_kind = 'catalogue'
       and i.source_table = 'suppliers.catalogue'
       and left(i.source_ref, length(v_prefix)) = v_prefix
       and i.source_ref <> all (v_refs);
  end if;

  return v_n;
end
$$;

-- ted_awards row -> ted_notice / supply, ref ted:<publication_number>/<winner_key>, text from the joined notice.
create or replace function public.product_bridge_upsert_award(p_publication_number text, p_winner_key text)
returns uuid
language plpgsql
set search_path = ''
as $$
declare
  a      public.ted_awards%rowtype;
  n      record;
  v_tail text;
begin
  select * into a from public.ted_awards where publication_number = p_publication_number and winner_key = p_winner_key;
  if not found then
    return null;
  end if;
  select t.publication_number, t.notice_type, t.publication_date, t.title_en, t.title_original, t.description,
         t.cpv_codes, t.buyer_name, t.buyer_country
    into n
    from public.ted_notices t where t.publication_number = a.publication_number;
  if not found then
    return null;  -- notice not there yet; the ted_notices trigger creates the item when it arrives
  end if;
  v_tail := a.publication_number || '/' || a.winner_key;

  return public.product_bridge_upsert_item(
    'supply', 'ted_notice', 'ted:' || v_tail, 'ted_awards', v_tail, null,
    public.product_bridge_match_supplier(a.winner_identifier, a.winner_name, a.winner_country), null,
    a.publication_number, null,
    a.winner_name,
    n.title_en,
    coalesce(public.product_bridge_join_text(n.title_en, n.title_original, n.description),
             'TED notice ' || a.publication_number),
    n.cpv_codes,
    coalesce(a.decision_date, n.publication_date)::timestamptz,
    'document_verified',
    jsonb_build_object('award', jsonb_strip_nulls(jsonb_build_object(
      'winner_identifier', a.winner_identifier,
      'winner_country', a.winner_country,
      'winner_size', a.winner_size,
      'winner_owner_nationality', a.winner_owner_nationality,
      'decision_date', a.decision_date,
      'notice_value', a.notice_value,
      'currency', a.currency,
      'co_winners', a.co_winners,
      'notice_type', n.notice_type,
      'buyer_name', n.buyer_name,
      'buyer_country', n.buyer_country))));
end
$$;

-- Re-upserts every award item of one publication (called when the notice row changes).
create or replace function public.product_bridge_refresh_awards(p_publication_number text)
returns integer
language plpgsql
set search_path = ''
as $$
declare
  r   record;
  v_n integer := 0;
begin
  for r in select a.winner_key from public.ted_awards a where a.publication_number = p_publication_number loop
    if public.product_bridge_upsert_award(p_publication_number, r.winner_key) is not null then
      v_n := v_n + 1;
    end if;
  end loop;
  return v_n;
end
$$;

-- ted_notices kind='competition' -> ted_notice / demand, ref ted:<publication_number>.
create or replace function public.product_bridge_upsert_notice(p_publication_number text)
returns uuid
language plpgsql
set search_path = ''
as $$
declare
  n record;
begin
  select t.publication_number, t.kind, t.notice_type, t.procedure_type, t.publication_date, t.title_en,
         t.title_original, t.description, t.original_language, t.buyer_name, t.buyer_identifier, t.buyer_country,
         t.buyer_legal_type, t.main_activity, t.cpv_codes, t.focus_classes, t.estimated_value, t.estimated_currency,
         t.deadline_tender, t.deadline_request, t.place_of_performance, t.ted_url
    into n
    from public.ted_notices t where t.publication_number = p_publication_number;
  if not found or n.kind is distinct from 'competition' then
    return null;
  end if;

  return public.product_bridge_upsert_item(
    'demand', 'ted_notice', 'ted:' || n.publication_number, 'ted_notices', n.publication_number, null,
    null, public.product_bridge_match_buyer(n.buyer_identifier, n.buyer_name, n.buyer_country),
    n.publication_number, null,
    n.buyer_name,
    n.title_en,
    coalesce(public.product_bridge_join_text(n.title_en, n.title_original, n.description),
             'TED notice ' || n.publication_number),
    n.cpv_codes,
    n.publication_date::timestamptz,
    'document_verified',
    jsonb_build_object('notice', jsonb_strip_nulls(jsonb_build_object(
      'notice_type', n.notice_type,
      'procedure_type', n.procedure_type,
      'original_language', n.original_language,
      'buyer_identifier', n.buyer_identifier,
      'buyer_country', n.buyer_country,
      'buyer_legal_type', n.buyer_legal_type,
      'main_activity', n.main_activity,
      'focus_classes', n.focus_classes,
      'estimated_value', n.estimated_value,
      'estimated_currency', n.estimated_currency,
      'deadline_tender', n.deadline_tender,
      'deadline_request', n.deadline_request,
      'place_of_performance', n.place_of_performance,
      'ted_url', n.ted_url))));
end
$$;

-- Re-matches supplier_id of award items and buyer_id of demand items (suppliers/buyers can arrive after TED rows).
-- Returns the number of items whose link changed.
create or replace function public.product_bridge_relink()
returns integer
language plpgsql
set search_path = ''
as $$
declare
  v_s integer;
  v_b integer;
begin
  update public.product_items i
     set supplier_id = m.sid, updated_at = now()
    from (select i2.id,
                 public.product_bridge_match_supplier(a.winner_identifier, a.winner_name, a.winner_country) as sid
            from public.product_items i2
            join public.ted_awards a
              on a.publication_number = i2.publication_number
             and a.publication_number || '/' || a.winner_key = i2.source_pk
           where i2.source_table = 'ted_awards') m
   where i.id = m.id
     and i.supplier_id is distinct from m.sid;
  get diagnostics v_s = row_count;

  update public.product_items i
     set buyer_id = m.bid, updated_at = now()
    from (select i2.id,
                 public.product_bridge_match_buyer(t.buyer_identifier, t.buyer_name, t.buyer_country) as bid
            from public.product_items i2
            join public.ted_notices t on t.publication_number = i2.source_pk
           where i2.source_table = 'ted_notices') m
   where i.id = m.id
     and i.buyer_id is distinct from m.bid;
  get diagnostics v_b = row_count;

  return v_s + v_b;
end
$$;

-- ---------------------------------------------------------------------------------------------------------
-- 4. Orphan cleanup + backfill
-- ---------------------------------------------------------------------------------------------------------

-- Deletes bridged items whose source row no longer exists and that the bridge has not seen for 1 hour.
create or replace function public.product_bridge_cleanup_orphans()
returns integer
language plpgsql
set search_path = ''
as $$
declare
  v_n integer;
begin
  delete from public.product_items i
   where i.source_table in ('supplier_contracts', 'suppliers.catalogue', 'ted_notices', 'ted_awards')
     and coalesce(i.source_seen_at, i.updated_at) < now() - interval '1 hour'
     and not (case i.source_table
       when 'supplier_contracts' then exists (
         select 1 from public.supplier_contracts c
          where c.notice_number = i.publication_number
            and c.supplier_id || '/' || c.notice_number || '/' || coalesce(nullif(btrim(c.lot_id), ''), 'notice') = i.source_pk)
       when 'suppliers.catalogue' then exists (
         select 1
           from public.suppliers s,
                jsonb_array_elements(case when jsonb_typeof(s.catalogue->'items') = 'array' then s.catalogue->'items'
                                          else '[]'::jsonb end) e(item)
          where s.id = i.supplier_id
            and jsonb_typeof(e.item) = 'object'
            and public.product_bridge_catalogue_ref(s.id, e.item) = i.source_ref)
       when 'ted_notices' then exists (
         select 1 from public.ted_notices t where t.publication_number = i.source_pk and t.kind = 'competition')
       when 'ted_awards' then exists (
         select 1 from public.ted_awards a
          where a.publication_number = i.publication_number
            and a.publication_number || '/' || a.winner_key = i.source_pk)
       else true
     end);
  get diagnostics v_n = row_count;
  return v_n;
end
$$;

-- Upserts every existing row of the four sources. Returns counts per source.
create or replace function public.product_bridge_backfill()
returns jsonb
language plpgsql
set search_path = ''
as $$
declare
  r          record;
  v_contract integer := 0;
  v_catalog  integer := 0;
  v_notice   integer := 0;
  v_award    integer := 0;
  v_errors   integer := 0;
begin
  for r in select c.id from public.supplier_contracts c order by c.id loop
    begin
      if public.product_bridge_upsert_contract(r.id) is not null then v_contract := v_contract + 1; end if;
    exception when others then
      v_errors := v_errors + 1;
      raise warning 'product_bridge_backfill: supplier_contracts id=% failed: % (%)', r.id, sqlerrm, sqlstate;
    end;
  end loop;

  for r in select s.id from public.suppliers s order by s.id loop
    begin
      v_catalog := v_catalog + public.product_bridge_sync_catalogue(r.id);
    exception when others then
      v_errors := v_errors + 1;
      raise warning 'product_bridge_backfill: suppliers.catalogue id=% failed: % (%)', r.id, sqlerrm, sqlstate;
    end;
  end loop;

  for r in select t.publication_number from public.ted_notices t where t.kind = 'competition' order by 1 loop
    begin
      if public.product_bridge_upsert_notice(r.publication_number) is not null then v_notice := v_notice + 1; end if;
    exception when others then
      v_errors := v_errors + 1;
      raise warning 'product_bridge_backfill: ted_notices % failed: % (%)', r.publication_number, sqlerrm, sqlstate;
    end;
  end loop;

  for r in select a.publication_number, a.winner_key from public.ted_awards a order by 1, 2 loop
    begin
      if public.product_bridge_upsert_award(r.publication_number, r.winner_key) is not null then v_award := v_award + 1; end if;
    exception when others then
      v_errors := v_errors + 1;
      raise warning 'product_bridge_backfill: ted_awards %/% failed: % (%)', r.publication_number, r.winner_key, sqlerrm, sqlstate;
    end;
  end loop;

  return jsonb_build_object(
    'supplier_contracts', v_contract,
    'suppliers.catalogue', v_catalog,
    'ted_notices', v_notice,
    'ted_awards', v_award,
    'errors', v_errors);
end
$$;

-- ---------------------------------------------------------------------------------------------------------
-- 5. Trigger functions (security definer so the writer's role does not matter; never raise)
-- ---------------------------------------------------------------------------------------------------------

create or replace function public.product_bridge_trg_supplier_contracts()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  begin
    perform public.product_bridge_upsert_contract(new.id);
  exception when others then
    raise warning 'product_bridge: supplier_contracts id=% not bridged: % (%)', new.id, sqlerrm, sqlstate;
  end;
  return null;
end
$$;

create or replace function public.product_bridge_trg_suppliers()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  begin
    if tg_op = 'INSERT' then
      perform public.product_bridge_sync_catalogue(new.id);
      perform public.product_bridge_relink();
    else
      if old.catalogue is distinct from new.catalogue then
        perform public.product_bridge_sync_catalogue(new.id);
      end if;
      if old.name is distinct from new.name then
        update public.product_items
           set org_ref = new.name, updated_at = now()
         where supplier_id = new.id
           and source_table in ('supplier_contracts', 'suppliers.catalogue');
      end if;
      if old.identifier is distinct from new.identifier
         or old.legal_name is distinct from new.legal_name
         or old.name is distinct from new.name
         or old.country is distinct from new.country then
        perform public.product_bridge_relink();
      end if;
    end if;
  exception when others then
    raise warning 'product_bridge: suppliers id=% not bridged: % (%)', new.id, sqlerrm, sqlstate;
  end;
  return null;
end
$$;

create or replace function public.product_bridge_trg_ted_notices()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  begin
    perform public.product_bridge_upsert_notice(new.publication_number);
    perform public.product_bridge_refresh_awards(new.publication_number);
  exception when others then
    raise warning 'product_bridge: ted_notices % not bridged: % (%)', new.publication_number, sqlerrm, sqlstate;
  end;
  return null;
end
$$;

create or replace function public.product_bridge_trg_ted_awards()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  begin
    perform public.product_bridge_upsert_award(new.publication_number, new.winner_key);
  exception when others then
    raise warning 'product_bridge: ted_awards %/% not bridged: % (%)', new.publication_number, new.winner_key, sqlerrm, sqlstate;
  end;
  return null;
end
$$;

-- ---------------------------------------------------------------------------------------------------------
-- 6. Triggers (AFTER INSERT OR UPDATE, FOR EACH ROW; no DELETE triggers on purpose)
-- ---------------------------------------------------------------------------------------------------------

drop trigger if exists product_bridge_supplier_contracts on public.supplier_contracts;
create trigger product_bridge_supplier_contracts
  after insert or update on public.supplier_contracts
  for each row execute function public.product_bridge_trg_supplier_contracts();

drop trigger if exists product_bridge_suppliers_ins on public.suppliers;
create trigger product_bridge_suppliers_ins
  after insert on public.suppliers
  for each row execute function public.product_bridge_trg_suppliers();

drop trigger if exists product_bridge_suppliers_upd on public.suppliers;
create trigger product_bridge_suppliers_upd
  after update on public.suppliers
  for each row
  when (old.catalogue is distinct from new.catalogue
        or old.name is distinct from new.name
        or old.legal_name is distinct from new.legal_name
        or old.identifier is distinct from new.identifier
        or old.country is distinct from new.country)
  execute function public.product_bridge_trg_suppliers();

drop trigger if exists product_bridge_ted_notices on public.ted_notices;
create trigger product_bridge_ted_notices
  after insert or update on public.ted_notices
  for each row execute function public.product_bridge_trg_ted_notices();

drop trigger if exists product_bridge_ted_awards on public.ted_awards;
create trigger product_bridge_ted_awards
  after insert or update on public.ted_awards
  for each row execute function public.product_bridge_trg_ted_awards();

-- ---------------------------------------------------------------------------------------------------------
-- 7. Privileges: internal functions, not exposed through PostgREST
-- ---------------------------------------------------------------------------------------------------------

do $$
declare
  f regprocedure;
begin
  for f in
    select p.oid::regprocedure
      from pg_proc p
     where p.pronamespace = 'public'::regnamespace
       and p.proname like 'product\_bridge\_%'
  loop
    execute format('revoke all on function %s from public, anon, authenticated', f);
    execute format('grant execute on function %s to service_role', f);
  end loop;
end
$$;

-- ---------------------------------------------------------------------------------------------------------
-- 8. Hourly orphan cleanup + relink (ted-sync runs at :17; this runs at :47)
-- ---------------------------------------------------------------------------------------------------------

select cron.schedule(
  'product-bridge-cleanup',
  '47 * * * *',
  $cron$ select public.product_bridge_cleanup_orphans(); select public.product_bridge_relink(); $cron$
);
