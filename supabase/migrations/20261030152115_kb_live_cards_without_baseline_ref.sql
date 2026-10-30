-- Live award/tender cards stay live unless the active baseline has a card under the same ref.
-- Bug: with baseline_v1 active, kb_refresh drops every pre-cutoff ted_live award and closed tender card, but the
-- baseline keeps one award card per winner per procedure (latest result notice) and one tender card per procedure.
-- 143 of 1,036 award cards (104 publications) would lose their only card at the next run, and 306 closed
-- competition notices of ted_notices had already lost theirs.
-- Rule now: a pre-cutoff TED row leaves ted_live only when a card with the same ref exists in the active snapshot
-- (kb_snapshots.active), so each ref is findable in exactly one layer. Exception kept on purpose: competition
-- notices still open at the cutoff stay live even when the baseline has them (today only 403428-2025, a
-- qualification system open until 2031). Only the cutoff predicates change; kb_refresh is unchanged.

create or replace function public.kb_build_award(p_cutoff date default null, p_since timestamptz default null)
returns setof public.kb_chunk_src
language sql stable security definer set search_path = '' as $$
  select
    'award'::text,
    'award:' || a.publication_number || '/' || a.winner_key,
    'award:' || a.publication_number || '/' || a.winner_key,
    left(a.winner_name || ' won ' || a.publication_number || ' · '
         || coalesce(nullif(btrim(n.title_en), ''), nullif(btrim(n.title_original), ''), 'award'), 300),
    left(concat_ws(E'\n',
      'AWARD ' || a.publication_number || ' · ' || coalesce(n.buyer_country, 'country not stated') || ' · buyer '
        || coalesce(nullif(btrim(n.buyer_name), ''), 'not stated') || ' → winner ' || a.winner_name
        || ' (' || coalesce(a.winner_country, 'country not stated') || ', ' || coalesce(a.winner_size, 'size not stated') || ')',
      'What: ' || public.kb_what(i.family_id, x.class_id, x.calibre),
      coalesce(nullif(btrim(n.title_en), ''), nullif(btrim(n.title_original), '')),
      nullif(left(btrim(n.description), 600), ''),
      'Notice value: ' || coalesce(public.kb_fmt_money(a.notice_value, a.currency), 'not published')
        || ' (notice total, ' || coalesce(a.co_winners::text, '?') || ' winner(s))',
      'Decided: ' || coalesce(a.decision_date::text, 'not stated')
    ), 2000),
    jsonb_strip_nulls(jsonb_build_object(
      'publication_number', a.publication_number, 'ted_url', n.ted_url, 'notice_type', n.notice_type,
      'published', n.publication_date, 'buyer', n.buyer_name, 'buyer_id', x.buyer_id, 'country', n.buyer_country,
      'procedure_type', n.procedure_type, 'legal_basis', n.legal_basis,
      'winner', a.winner_name, 'winner_identifier', a.winner_identifier, 'winner_country', a.winner_country,
      'winner_size', a.winner_size, 'winner_owner_nationality', a.winner_owner_nationality, 'supplier_id', i.supplier_id,
      'class_id', x.class_id, 'family_id', i.family_id, 'calibre', x.calibre,
      'classification_method', i.classification_method, 'confidence', round(i.confidence::numeric, 2),
      'needs_review', i.needs_review,
      'notice_value', a.notice_value, 'currency', a.currency, 'value_basis', 'notice_total',
      'co_winners', a.co_winners, 'decision_date', a.decision_date, 'bids_received', n.bids_received
    )),
    a.publication_number, i.supplier_id, x.buyer_id,
    case when i.id is null then '{}'::uuid[] else array[i.id] end,
    x.class_id, i.family_id, x.calibre, coalesce(n.cpv_codes, '{}'::text[]), public.kb_country(n.buyer_country),
    coalesce(a.decision_date, n.publication_date), 'ted_live'::text, 'public'::text, '{}'::text[]
  from public.ted_awards a
  join public.ted_notices n on n.publication_number = a.publication_number
  left join public.product_items i
    on i.source_kind = 'ted_notice' and i.source_ref = 'ted:' || a.publication_number || '/' || a.winner_key
  cross join lateral (
    select coalesce(i.class_id, public.kb_cpv_class(n.cpv_codes)) as class_id,
           nullif(btrim(i.attrs ->> 'calibre'), '') as calibre,
           public.product_bridge_match_buyer(n.buyer_identifier, n.buyer_name, n.buyer_country) as buyer_id
  ) x
  where (p_cutoff is null
         or n.publication_date > p_cutoff
         -- pre-cutoff award without a card under the same ref in the active baseline: stays live, so every
         -- ted_awards row remains findable by its ref in exactly one layer
         or not exists (
           select 1
           from public.kb_chunks k
           join public.kb_snapshots s on s.id = k.snapshot_id and s.active
           where k.kind = 'award' and k.layer = 'baseline'
             and k.source_key = 'award:' || a.publication_number || '/' || a.winner_key
             and k.ref = 'award:' || a.publication_number || '/' || a.winner_key))
    and (p_since is null or a.updated_at >= p_since or n.updated_at >= p_since or i.updated_at >= p_since)
$$;

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
         or greatest(coalesce(n.deadline_tender, '-infinity'), coalesce(n.deadline_request, '-infinity')) >= p_cutoff
         -- closed before the cutoff but no card under the same ref in the active baseline (the baseline keeps one
         -- tender card per procedure): stays live, so the notice remains findable by its ref
         or not exists (
           select 1
           from public.kb_chunks k
           join public.kb_snapshots s on s.id = k.snapshot_id and s.active
           where k.kind = 'tender' and k.layer = 'baseline'
             and k.source_key = 'ted:' || n.publication_number
             and k.ref = 'ted:' || n.publication_number))
    and (p_since is null or n.updated_at >= p_since or i.updated_at >= p_since)
$$;
