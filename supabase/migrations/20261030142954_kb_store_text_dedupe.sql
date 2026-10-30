-- WS1 follow-up: drop body lines that only repeat the title (215 tenders whose description equals the title,
-- 78 contracts whose description equals the lot title, all 339 catalogue items whose text starts with the product
-- name). Same builders as 20261030142503_kb_store otherwise; grants are kept by create or replace.
-- Applied before the first embedding run, so nothing is re-embedded.

create or replace function public.kb_build_tender(p_cutoff date default null, p_since timestamptz default null)
returns setof public.kb_chunk_src
language sql stable security definer set search_path = '' as $$
  select
    'tender'::text,
    'ted:' || n.publication_number,
    'ted:' || n.publication_number,
    left(coalesce(nullif(btrim(n.title_en), ''), nullif(btrim(n.title_original), ''), 'Tender ' || n.publication_number), 300),
    left(concat_ws(E'\n',
      'TENDER ' || n.publication_number || ' · ' || coalesce(n.buyer_country, 'country not stated') || ' · '
        || coalesce(nullif(btrim(n.buyer_name), ''), 'buyer not stated') || ' · '
        || coalesce(n.procedure_type, 'procedure not stated') || ' · legal basis ' || coalesce(n.legal_basis, 'not stated'),
      'What: ' || public.kb_what(i.family_id, x.class_id, x.calibre),
      'Title: ' || coalesce(nullif(btrim(n.title_en), ''), 'not published'),
      case when nullif(btrim(n.title_original), '') is not null and n.title_original is distinct from n.title_en
           then 'Original: ' || btrim(n.title_original) end,
      case when btrim(n.description) is distinct from btrim(n.title_en)
            and btrim(n.description) is distinct from btrim(n.title_original)
           then nullif(left(btrim(n.description), 1200), '') end,
      'Deadline: ' || coalesce(public.kb_fmt_ts(coalesce(n.deadline_tender, n.deadline_request))
        || case when n.deadline_tender is not null then ' (tenders)' else ' (requests to participate)' end, 'not published'),
      'Estimated value: ' || coalesce(public.kb_fmt_money(n.estimated_value, n.estimated_currency)
        || coalesce(' (' || n.estimated_value_source || ')', ''), 'not published')
    ), 2000),
    jsonb_strip_nulls(jsonb_build_object(
      'publication_number', n.publication_number, 'ted_url', n.ted_url, 'notice_type', n.notice_type,
      'published', n.publication_date, 'buyer', n.buyer_name, 'buyer_id', i.buyer_id, 'country', n.buyer_country,
      'buyer_legal_type', n.buyer_legal_type, 'procedure_type', n.procedure_type, 'legal_basis', n.legal_basis,
      'class_id', x.class_id, 'family_id', i.family_id, 'calibre', x.calibre,
      'classification_method', i.classification_method, 'confidence', round(i.confidence::numeric, 2),
      'needs_review', i.needs_review,
      'estimated_value', n.estimated_value, 'currency', n.estimated_currency, 'value_source', n.estimated_value_source,
      'deadline_tender', n.deadline_tender, 'deadline_request', n.deadline_request,
      'next_deadline', coalesce(n.deadline_tender, n.deadline_request),
      'deadline_kind', case when n.deadline_tender is not null then 'tenders'
                            when n.deadline_request is not null then 'requests_to_participate' end,
      'is_open', greatest(coalesce(n.deadline_tender, '-infinity'), coalesce(n.deadline_request, '-infinity')) >= now()
    )),
    n.publication_number, null::text, i.buyer_id,
    case when i.id is null then '{}'::uuid[] else array[i.id] end,
    x.class_id, i.family_id, x.calibre, coalesce(n.cpv_codes, '{}'::text[]), public.kb_country(n.buyer_country),
    n.publication_date, 'ted_live'::text, 'public'::text, '{}'::text[]
  from public.ted_notices n
  left join public.product_items i on i.source_kind = 'ted_notice' and i.source_ref = 'ted:' || n.publication_number
  cross join lateral (
    select coalesce(i.class_id, public.kb_cpv_class(n.cpv_codes)) as class_id,
           nullif(btrim(i.attrs ->> 'calibre'), '') as calibre
  ) x
  where n.kind = 'competition'
    and (p_cutoff is null or n.publication_date > p_cutoff)
    and (p_since is null or n.updated_at >= p_since or i.updated_at >= p_since)
$$;

create or replace function public.kb_build_contract(p_since timestamptz default null)
returns setof public.kb_chunk_src
language sql stable security definer set search_path = '' as $$
  select
    'contract'::text,
    x.ref, x.ref,
    left(coalesce(s.name, c.supplier_id) || ' · ' || coalesce(x.title, c.notice_number), 300),
    left(concat_ws(E'\n',
      'CONTRACT ' || coalesce(s.name, c.supplier_id) || ' won lot ' || x.lot || ' of ' || c.notice_number
        || ' · buyer ' || coalesce(nullif(btrim(c.buyer), ''), 'not stated') || ' (' || coalesce(c.buyer_country, 'country not stated') || ')',
      'What: ' || public.kb_what(i.family_id, x.class_id, x.calibre),
      x.title,
      case when btrim(c.description) is distinct from x.title then nullif(left(btrim(c.description), 800), '') end,
      'Awarded: ' || coalesce(public.kb_fmt_money(c.awarded_value, c.awarded_currency), 'value not published')
        || ' · quantity ' || coalesce(nullif(btrim(c.quantity), ''), 'not stated')
        || ' · tenders received ' || coalesce(c.tenders_received::text, 'not stated')
        || ' · ' || coalesce(c.award_date::text, 'award date not stated')
        || case when c.framework_agreement then ' · framework agreement' else '' end
    ), 2000),
    jsonb_strip_nulls(jsonb_build_object(
      'supplier_id', c.supplier_id, 'supplier', s.name, 'notice_number', c.notice_number, 'lot_id', c.lot_id,
      'notice_type', c.notice_type, 'ted_url', c.ted_url, 'buyer', c.buyer, 'country', c.buyer_country,
      'class_id', x.class_id, 'family_id', i.family_id, 'calibre', x.calibre,
      'classification_method', i.classification_method, 'confidence', round(i.confidence::numeric, 2),
      'needs_review', i.needs_review,
      'awarded_value', c.awarded_value, 'currency', c.awarded_currency, 'value_basis', 'awarded_lot_value',
      'quantity', nullif(btrim(c.quantity), ''),
      'tenders_received', c.tenders_received, 'framework_agreement', c.framework_agreement,
      'co_winners', c.co_winners, 'award_date', c.award_date, 'published', c.published,
      'contract_signed', c.contract_signed, 'winner_confirmed', c.winner_confirmed
    )),
    c.notice_number, c.supplier_id, null::text,
    case when i.id is null then '{}'::uuid[] else array[i.id] end,
    x.class_id, i.family_id, x.calibre, coalesce(c.cpv_codes, '{}'::text[]), public.kb_country(c.buyer_country),
    coalesce(c.award_date, c.published), 'ted_live'::text, 'public'::text, '{}'::text[]
  from public.supplier_contracts c
  left join public.suppliers s on s.id = c.supplier_id
  cross join lateral (
    select coalesce(nullif(btrim(c.lot_id), ''), 'notice') as lot,
           'contract:' || c.supplier_id || '/' || c.notice_number || '/' || coalesce(nullif(btrim(c.lot_id), ''), 'notice') as ref,
           coalesce(nullif(btrim(c.title), ''), nullif(btrim(c.notice_title), ''), nullif(btrim(c.contract_title), '')) as title
  ) x0
  left join public.product_items i on i.source_kind = 'ted_lot' and i.source_ref = x0.ref
  cross join lateral (
    select x0.lot, x0.ref, x0.title,
           coalesce(i.class_id, public.kb_cpv_class(c.cpv_codes)) as class_id,
           nullif(btrim(i.attrs ->> 'calibre'), '') as calibre
  ) x
  where p_since is null or i.updated_at >= p_since or s.updated_at >= p_since or i.id is null
$$;

create or replace function public.kb_build_catalogue_item(p_since timestamptz default null)
returns setof public.kb_chunk_src
language sql stable security definer set search_path = '' as $$
  select
    'catalogue_item'::text,
    i.source_ref, i.source_ref,
    left(coalesce(s.name, i.org_ref, i.supplier_id, 'supplier') || ' · ' || coalesce(nullif(btrim(i.title), ''), 'catalogue item'), 300),
    left(concat_ws(E'\n',
      'CATALOGUE ' || coalesce(s.name, i.org_ref, i.supplier_id, 'supplier not stated') || ' · '
        || public.kb_what(i.family_id, x.class_id, x.calibre),
      coalesce(nullif(btrim(i.title), ''), 'untitled item'),
      nullif(left(btrim(case when nullif(btrim(i.title), '') is not null and starts_with(btrim(i.source_text), btrim(i.title))
                             then substr(btrim(i.source_text), length(btrim(i.title)) + 1) else i.source_text end), 800), ''),
      'Commercial: ' || coalesce(public.kb_jsonb_text(i.extra -> 'catalogue' -> 'commercial', 400), 'not stated'),
      'Evidence: ' || coalesce(i.verification_status, 'not stated')
    ), 2000),
    jsonb_strip_nulls(jsonb_build_object(
      'supplier_id', i.supplier_id, 'supplier', coalesce(s.name, i.org_ref), 'item_id', i.id,
      'class_id', x.class_id, 'family_id', i.family_id, 'calibre', x.calibre, 'attrs', i.attrs,
      'classification_method', i.classification_method, 'confidence', round(i.confidence::numeric, 2),
      'needs_review', i.needs_review,
      'category', i.extra -> 'catalogue' ->> 'category', 'unit', i.extra -> 'catalogue' ->> 'unit',
      'verification_status', i.verification_status, 'commercial', i.extra -> 'catalogue' -> 'commercial',
      'track_record', i.extra -> 'catalogue' -> 'track_record'
    )),
    null::text, i.supplier_id, null::text, array[i.id],
    x.class_id, i.family_id, x.calibre, coalesce(i.cpv_codes, '{}'::text[]), public.kb_country(s.country),
    i.observed_at::date, 'platform'::text,
    case when i.visibility = 'public' then 'public' else 'parties' end,
    case when i.visibility = 'public' or i.supplier_id is null then '{}'::text[] else array[i.supplier_id] end
  from public.product_items i
  left join public.suppliers s on s.id = i.supplier_id
  cross join lateral (
    select coalesce(i.class_id, public.kb_cpv_class(i.cpv_codes)) as class_id,
           nullif(btrim(i.attrs ->> 'calibre'), '') as calibre
  ) x
  where i.source_kind = 'catalogue'
    and (p_since is null or i.updated_at >= p_since or s.updated_at >= p_since)
$$;
