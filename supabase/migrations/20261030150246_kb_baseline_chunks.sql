-- WS8 8.7 + 8.2 on kb_chunks: baseline cards (tender with outcome line, award per winner, pattern) and the
-- immutability guard for rows with layer = 'baseline'. kb_chunks is owned by WS1: this migration only adds
-- triggers and inserts baseline rows; it does not alter the table.
--
-- Cards are written for procedures that the ontology places in a class (the population of kb_baseline_stats).
-- Defence-directive procedures outside the weapons taxonomy stay in kb_baseline_procedures without a card
-- (database size and embedding quota).

-- Guard v2: stored generated columns (content_hash, fts) are not yet computed in a BEFORE trigger, so they are
-- left out of the comparison too; they derive from title/content, which are compared. Net effect: on a frozen
-- baseline row only embedding, embedding_model, embedded_hash and embedded_at may change.
create or replace function public.kb_baseline_frozen_guard()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  c_embedding constant text[] := array['embedding', 'embedding_model', 'embedded_hash', 'embedded_at',
                                       'content_hash', 'fts'];
  v_old text;
  v_new text;
  v_locked text;
  v_exists boolean;
begin
  if tg_op = 'TRUNCATE' then
    execute format(
      'select exists (select 1 from %I.%I t join public.kb_snapshots s on s.id = t.snapshot_id
                      where s.status <> ''building'' %s)',
      tg_table_schema, tg_table_name,
      case when tg_table_name = 'kb_chunks' then 'and t.layer = ''baseline''' else '' end)
      into v_exists;
    if v_exists then
      raise exception 'kb_baseline_frozen_guard: %.% holds rows of a frozen snapshot; TRUNCATE is not allowed',
        tg_table_schema, tg_table_name using errcode = '55000';
    end if;
    return null;
  end if;

  if tg_table_name = 'kb_chunks' then
    if tg_op in ('UPDATE', 'DELETE') and old.layer = 'baseline' then
      v_old := old.snapshot_id;
    end if;
    if tg_op in ('INSERT', 'UPDATE') and new.layer = 'baseline' then
      v_new := new.snapshot_id;
    end if;
    if tg_op = 'UPDATE' and v_old is not null and new.layer = 'baseline'
       and (to_jsonb(new) - c_embedding) = (to_jsonb(old) - c_embedding) then
      return new;                                  -- embedding-only update (kb_embed_store)
    end if;
  else
    if tg_op in ('UPDATE', 'DELETE') then
      v_old := old.snapshot_id;
    end if;
    if tg_op in ('INSERT', 'UPDATE') then
      v_new := new.snapshot_id;
    end if;
  end if;

  if v_old is null and v_new is null then
    return case when tg_op = 'DELETE' then old else new end;
  end if;

  select string_agg(distinct s.id || ' (' || s.status || ')', ', ')
    into v_locked
  from public.kb_snapshots s
  where s.id in (v_old, v_new) and s.status <> 'building';

  if v_locked is not null then
    raise exception 'kb_baseline_frozen_guard: snapshot % is immutable (% on %.%); build a new snapshot_id instead',
      v_locked, tg_op, tg_table_schema, tg_table_name using errcode = '55000';
  end if;

  return case when tg_op = 'DELETE' then old else new end;
end
$$;
revoke all on function public.kb_baseline_frozen_guard() from public, anon, authenticated;

-- Triggers on kb_chunks fire only for baseline rows (WHEN), so ted_live / platform writes pay nothing.
drop trigger if exists kb_baseline_frozen_guard_ins on public.kb_chunks;
create trigger kb_baseline_frozen_guard_ins before insert on public.kb_chunks
  for each row when (new.layer = 'baseline') execute function public.kb_baseline_frozen_guard();
drop trigger if exists kb_baseline_frozen_guard_upd on public.kb_chunks;
create trigger kb_baseline_frozen_guard_upd before update on public.kb_chunks
  for each row when (old.layer = 'baseline' or new.layer = 'baseline') execute function public.kb_baseline_frozen_guard();
drop trigger if exists kb_baseline_frozen_guard_del on public.kb_chunks;
create trigger kb_baseline_frozen_guard_del before delete on public.kb_chunks
  for each row when (old.layer = 'baseline') execute function public.kb_baseline_frozen_guard();
drop trigger if exists kb_baseline_frozen_guard_truncate on public.kb_chunks;
create trigger kb_baseline_frozen_guard_truncate before truncate on public.kb_chunks
  for each statement execute function public.kb_baseline_frozen_guard();

-- Human-readable BT-144 non-award reasons.
create or replace function public.kb_baseline_reason_text(p_codes text[])
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select nullif(string_agg(case c when 'no-rece' then 'no tenders or requests received'
                                  when 'all-rej' then 'all tenders rejected or withdrawn'
                                  when 'chan-need' then 'buyer''s needs changed'
                                  when 'ins-fund' then 'insufficient funds'
                                  when 'tch-pr-error' then 'technical or procedural error'
                                  when 'other' then 'other reason'
                                  else c end, '; ' order by o), '')
  from unnest(coalesce(p_codes, '{}'::text[])) with ordinality u(c, o)
$$;
revoke all on function public.kb_baseline_reason_text(text[]) from public, anon, authenticated;
grant execute on function public.kb_baseline_reason_text(text[]) to service_role;

-- Deterministic cards (same input -> same text; no now()). Rebuildable while the snapshot is 'building'.
create or replace function public.kb_baseline_build_chunks(p_snapshot text)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_status text;
  v_cutoff date;
  v_source text;
  v_tender integer;
  v_award integer;
  v_pattern integer;
begin
  select s.status, s.cutoff_date into v_status, v_cutoff from public.kb_snapshots s where s.id = p_snapshot;
  if v_status is distinct from 'building' then
    raise exception 'kb_baseline_build_chunks: snapshot % is % (must be building)', p_snapshot, coalesce(v_status, 'missing');
  end if;
  v_source := 'Source: frozen TED baseline ' || p_snapshot || ' (TED history up to ' || v_cutoff || ')';

  delete from public.kb_chunks k where k.layer = 'baseline' and k.snapshot_id = p_snapshot;

  -- tender cards: one per procedure, with the outcome line
  insert into public.kb_chunks (kind, source_key, ref, title, content, facts, publication_number, supplier_id, buyer_id,
                                item_ids, class_id, family_id, calibre, cpv_codes, country, occurred_at, layer, snapshot_id,
                                visibility, party_ids)
  select 'tender', 'ted:' || x.pub, 'ted:' || x.pub,
         left(coalesce(nullif(btrim(p.title_en), ''), nullif(btrim(p.title_original), ''), 'Procedure ' || x.pub), 300),
         left(concat_ws(E'\n',
           'TENDER ' || x.pub || ' · ' || coalesce(p.buyer_country, 'country not stated') || ' · '
             || coalesce(nullif(btrim(p.buyer_name), ''), 'buyer not stated') || ' · '
             || coalesce(p.procedure_type, 'procedure not stated') || ' · legal basis ' || coalesce(p.legal_basis, 'not stated'),
           'What: ' || public.kb_what(p.family_id, p.class_id, p.calibre),
           'Title: ' || coalesce(nullif(btrim(p.title_en), ''), 'not published'),
           case when nullif(btrim(p.title_original), '') is not null and p.title_original is distinct from p.title_en
                then 'Original: ' || btrim(p.title_original) end,
           case when btrim(p.description) is distinct from btrim(p.title_en)
                 and btrim(p.description) is distinct from btrim(p.title_original)
                then nullif(left(btrim(p.description), 1000), '') end,
           case when p.tender_publication_number is not null then
             'Deadline: ' || coalesce(public.kb_fmt_ts(p.deadline), 'not published') end,
           case when p.tender_publication_number is not null then
             'Estimated value: ' || coalesce(public.kb_fmt_money(p.estimated_value, p.estimated_currency), 'not published') end,
           'Outcome: ' || x.outcome_text,
           v_source
         ), 2000),
         jsonb_strip_nulls(jsonb_build_object(
           'snapshot_id', p.snapshot_id, 'layer', 'baseline', 'procedure_id', p.procedure_id,
           'publication_number', x.pub, 'tender_publication_number', p.tender_publication_number,
           'result_publication_numbers', to_jsonb(p.result_publication_numbers),
           'ted_url', 'https://ted.europa.eu/en/notice/-/detail/' || x.pub,
           'published', p.published, 'decided', p.decided, 'decided_basis', p.decided_basis,
           'outcome', p.outcome, 'result_status', p.result_status, 'outcome_basis', p.outcome_basis, 'overdue', p.overdue,
           'link_method', p.link_method,
           'buyer', p.buyer_name, 'buyer_id', x.buyer_id, 'country', p.buyer_country, 'buyer_legal_type', p.buyer_legal_type,
           'procedure_type', p.procedure_type, 'legal_basis', p.legal_basis,
           'class_id', p.class_id, 'family_id', p.family_id, 'calibre', p.calibre,
           'classification_method', p.classification_method, 'confidence', round(p.classification_confidence::numeric, 2),
           'estimated_value', p.estimated_value, 'estimated_currency', p.estimated_currency,
           'awarded_value', p.awarded_value, 'currency', p.currency,
           'value_basis', case when p.awarded_value is not null then 'notice_total' end,
           'days_to_award', p.days_to_award, 'bids_received', p.bids_received,
           'winners', case when jsonb_array_length(p.winners) > 0 then p.winners end, 'consortium', p.consortium,
           'n_lots', p.n_lots, 'quantity', p.quantity, 'quantity_unit', p.quantity_unit, 'unit_price', p.unit_price,
           'non_award_reasons', case when cardinality(p.non_award_reasons) > 0 then to_jsonb(p.non_award_reasons) end
         )),
         x.pub, null, x.buyer_id, '{}'::uuid[], p.class_id, p.family_id, p.calibre, p.cpv_codes,
         public.kb_country(p.buyer_country), coalesce(p.published, p.decided), 'baseline', p.snapshot_id, 'public', '{}'::text[]
  from public.kb_baseline_procedures p
  cross join lateral (
    select coalesce(p.tender_publication_number, p.result_publication_numbers[1], p.modification_publication_numbers[1]) as pub,
           public.product_bridge_match_buyer(p.buyer_identifier, p.buyer_name, p.buyer_country) as buyer_id,
           case
             when p.result_status = 'awarded' then
               'awarded to '
               || coalesce((select string_agg((w ->> 'name') || coalesce(' (' || (w ->> 'country') || ')', ''), ', ' order by o)
                            from jsonb_array_elements(p.winners) with ordinality a(w, o) where o <= 3)
                           || case when jsonb_array_length(p.winners) > 3
                                   then ' and ' || (jsonb_array_length(p.winners) - 3) || ' more' else '' end,
                           case when p.eforms then 'a winner not named in the notice'
                                else 'a winner not named in the TED Search API (legacy notice)' end)
               || ' for ' || coalesce(public.kb_fmt_money(p.awarded_value, p.currency) || ' (notice total)', 'a value not published')
               || coalesce(' after ' || p.days_to_award || ' days', '')
               || coalesce(', ' || trim_scale(p.bids_received) || ' bid(s)', ', bids not published')
               || coalesce(' · decided ' || p.decided, '')
               || case when p.outcome = 'result_only' then ' (tender notice not in the snapshot)' else '' end
             when p.result_status = 'unsuccessful' then
               'no award, unsuccessful (' || coalesce(public.kb_baseline_reason_text(p.non_award_reasons), 'no reason given') || ')'
               || coalesce(' · closed ' || p.decided, '')
             when p.result_status = 'cancelled' then
               'cancelled (' || coalesce(public.kb_baseline_reason_text(p.non_award_reasons), 'no reason given') || ')'
               || coalesce(' · closed ' || p.decided, '')
             when p.result_status = 'pending' then
               'result notice published, but no winner chosen yet (lots still open)'
             when p.result_status = 'unknown' then
               'result notice published; winner and value are not in the TED Search API (legacy notice)'
               || case when p.outcome = 'result_only' then ' (tender notice not in the snapshot)' else '' end
             when p.outcome_basis like 'no_linked_result%' then
               'no linked result notice (legacy notice: TED gives no procedure link, so a result may exist unlinked)'
             else
               'no result notice published by ' || v_cutoff
               || case when p.overdue then ' (more than 12 months after the deadline)' else '' end
           end as outcome_text
  ) x
  where p.snapshot_id = p_snapshot and p.class_id is not null;
  get diagnostics v_tender = row_count;

  -- award cards: one per winner and result notice (ref format of the ted_live layer: award:<pub>/<winner key>)
  insert into public.kb_chunks (kind, source_key, ref, title, content, facts, publication_number, supplier_id, buyer_id,
                                item_ids, class_id, family_id, calibre, cpv_codes, country, occurred_at, layer, snapshot_id,
                                visibility, party_ids)
  select 'award', a.key, a.key,
         left(a.winner_name || ' won ' || a.pub || ' · '
              || coalesce(nullif(btrim(p.title_en), ''), nullif(btrim(p.title_original), ''), 'award'), 300),
         left(concat_ws(E'\n',
           'AWARD ' || a.pub || ' · ' || coalesce(p.buyer_country, 'country not stated') || ' · buyer '
             || coalesce(nullif(btrim(p.buyer_name), ''), 'not stated') || ' → winner ' || a.winner_name
             || ' (' || coalesce(a.winner_country, 'country not stated') || ', ' || coalesce(a.winner_size, 'size not stated') || ')',
           'What: ' || public.kb_what(p.family_id, p.class_id, p.calibre),
           coalesce(nullif(btrim(p.title_en), ''), nullif(btrim(p.title_original), '')),
           nullif(left(btrim(p.description), 600), ''),
           'Notice value: ' || coalesce(public.kb_fmt_money(a.notice_value, a.currency), 'not published')
             || ' (notice total, ' || a.co_winners || ' winner(s))',
           'Decided: ' || coalesce(a.decision_date::text, 'not stated')
             || coalesce(' · ' || p.days_to_award || ' days after the tender ' || p.tender_publication_number, '')
             || coalesce(' · ' || trim_scale(a.bids_received) || ' bid(s)', ''),
           case when p.consortium and a.co_winners > 1 then 'Consortium: won jointly with '
             || (select string_agg(w ->> 'name', ', ') from jsonb_array_elements(a.notice_winners) w
                 where lower(btrim(w ->> 'name')) <> a.winner_key) end,
           v_source
         ), 2000),
         jsonb_strip_nulls(jsonb_build_object(
           'snapshot_id', p.snapshot_id, 'layer', 'baseline', 'procedure_id', p.procedure_id,
           'publication_number', a.pub, 'tender_publication_number', p.tender_publication_number,
           'ted_url', 'https://ted.europa.eu/en/notice/-/detail/' || a.pub,
           'buyer', p.buyer_name, 'country', p.buyer_country, 'procedure_type', p.procedure_type, 'legal_basis', p.legal_basis,
           'winner', a.winner_name, 'winner_identifier', a.winner_identifier, 'winner_country', a.winner_country,
           'winner_size', a.winner_size, 'winner_owner_nationality', a.winner_owner_nationality, 'supplier_id', a.supplier_id,
           'class_id', p.class_id, 'family_id', p.family_id, 'calibre', p.calibre,
           'classification_method', p.classification_method, 'confidence', round(p.classification_confidence::numeric, 2),
           'notice_value', a.notice_value, 'currency', a.currency, 'value_basis', 'notice_total',
           'co_winners', a.co_winners, 'decision_date', a.decision_date, 'bids_received', a.bids_received,
           'days_to_award', p.days_to_award, 'consortium', p.consortium
         )),
         a.pub, a.supplier_id, public.product_bridge_match_buyer(p.buyer_identifier, p.buyer_name, p.buyer_country),
         '{}'::uuid[], p.class_id, p.family_id, p.calibre, p.cpv_codes, public.kb_country(p.buyer_country),
         coalesce(a.decision_date, a.published), 'baseline', p.snapshot_id, 'public', '{}'::text[]
  from public.kb_baseline_procedures p
  cross join lateral (
    select distinct on (lower(btrim(w ->> 'name')))
           'award:' || n.publication_number || '/' || lower(btrim(w ->> 'name')) as key,
           n.publication_number as pub, n.publication_date as published,
           lower(btrim(w ->> 'name')) as winner_key, btrim(w ->> 'name') as winner_name,
           w ->> 'identifier' as winner_identifier, w ->> 'country' as winner_country, w ->> 'size' as winner_size,
           w ->> 'owner_nationality' as winner_owner_nationality,
           n.result_value as notice_value, n.result_currency as currency, n.decision_date, n.bids_received,
           jsonb_array_length(n.winners) as co_winners, n.winners as notice_winners,
           public.product_bridge_match_supplier(w ->> 'identifier', w ->> 'name', w ->> 'country') as supplier_id
    from public.kb_baseline_notices n, jsonb_array_elements(n.winners) w
    where n.snapshot_id = p.snapshot_id and n.publication_number = any (p.result_publication_numbers)
      and nullif(btrim(w ->> 'name'), '') is not null
    order by lower(btrim(w ->> 'name')), n.publication_date desc, n.publication_number desc
  ) a
  where p.snapshot_id = p_snapshot and p.class_id is not null;
  get diagnostics v_award = row_count;

  -- pattern cards
  insert into public.kb_chunks (kind, source_key, ref, title, content, facts, publication_number, supplier_id, buyer_id,
                                item_ids, class_id, family_id, calibre, cpv_codes, country, occurred_at, layer, snapshot_id,
                                visibility, party_ids)
  select 'pattern', 'pattern:' || t.snapshot_id || '/' || t.id, 'pattern:' || t.snapshot_id || '/' || t.id,
         left('Pattern ' || t.id || ' (' || t.kind || '): ' || t.statement, 300),
         left(concat_ws(E'\n',
           'PATTERN ' || t.id || ' · ' || t.kind || ' · ' || t.snapshot_id,
           'Scope: ' || coalesce(public.kb_jsonb_text(t.scope, 300), 'all families and countries'),
           t.statement,
           'Support: ' || t.support,
           case when cardinality(t.examples) > 0 then 'Examples: ' || array_to_string(t.examples, ', ') end,
           v_source
         ), 2000),
         jsonb_build_object('snapshot_id', t.snapshot_id, 'layer', 'baseline', 'pattern_id', t.id, 'kind', t.kind,
                            'scope', t.scope, 'support', t.support, 'examples', to_jsonb(t.examples), 'metrics', t.metrics),
         null, null, null, '{}'::uuid[],
         f.parent_id, case when f.level = 2 then f.id end, null, '{}'::text[],
         public.kb_country(t.scope ->> 'country'), v_cutoff, 'baseline', t.snapshot_id, 'public', '{}'::text[]
  from public.kb_patterns t
  left join public.product_families f on f.id = t.scope ->> 'family_id'
  where t.snapshot_id = p_snapshot;
  get diagnostics v_pattern = row_count;

  return jsonb_build_object('tender', v_tender, 'award', v_award, 'pattern', v_pattern);
end
$$;
revoke all on function public.kb_baseline_build_chunks(text) from public, anon, authenticated;
grant execute on function public.kb_baseline_build_chunks(text) to service_role;
