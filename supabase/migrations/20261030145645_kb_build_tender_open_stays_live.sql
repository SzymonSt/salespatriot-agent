-- Coordination WS8 <-> WS1 (agreed with the coordinator): once baseline_v1 is active, kb_refresh passes its
-- cutoff_date to the builders and drops every ted_live row the builder no longer returns. The baseline holds
-- only procedures closed at the cutoff, so a competition notice that is still open at the cutoff must stay in
-- ted_live regardless of its publication date. Only the cutoff predicate of kb_build_tender changes:
--   before: p_cutoff is null or n.publication_date > p_cutoff
--   after:  ... or greatest(deadline_tender, deadline_request) >= p_cutoff
create or replace function public.kb_build_tender(p_cutoff date default null, p_since timestamptz default null)
returns setof public.kb_chunk_src
language sql
stable
security definer
set search_path = ''
as $$
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
    and (p_cutoff is null
         or n.publication_date > p_cutoff
         -- still open at the cutoff: not in the frozen baseline, so it stays live
         or greatest(coalesce(n.deadline_tender, '-infinity'), coalesce(n.deadline_request, '-infinity')) >= p_cutoff)
    and (p_since is null or n.updated_at >= p_since or i.updated_at >= p_since)
$$;
