-- WS8 8.8: freeze a snapshot. Writes the manifest (cutoff, queries, counts per query and per table, taxonomy
-- version, content hash), sets status = 'frozen' and makes it the only active snapshot. From then on the guard
-- triggers block every insert, update and delete of its rows (embedding columns of baseline chunks excepted),
-- and kb_refresh builds ted_live only for notices after the cutoff (plus tenders still open at the cutoff).
--
-- content_sha256 = sha256 of the newline-joined, publication_number-ordered list "publication_number:md5(raw::text)"
-- over kb_baseline_notices of the snapshot. Recompute it with kb_baseline_content_hash(snapshot) to verify.
create or replace function public.kb_baseline_content_hash(p_snapshot text)
returns text
language sql
stable
security definer
set search_path = ''
as $$
  select encode(sha256(convert_to(coalesce(string_agg(n.publication_number || ':' || md5(n.raw::text), E'\n'
                                                      order by n.publication_number collate "C"), ''), 'UTF8')), 'hex')
  from public.kb_baseline_notices n
  where n.snapshot_id = p_snapshot
$$;
revoke all on function public.kb_baseline_content_hash(text) from public, anon, authenticated;
grant execute on function public.kb_baseline_content_hash(text) to service_role;

create or replace function public.kb_baseline_freeze(p_snapshot text, p_activate boolean default true)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  s public.kb_snapshots%rowtype;
  v_counts jsonb;
  v_manifest jsonb;
  v_hash text;
  v_tax int;
begin
  select * into s from public.kb_snapshots where id = p_snapshot for update;
  if not found then
    raise exception 'kb_baseline_freeze: unknown snapshot %', p_snapshot;
  end if;
  if s.status <> 'building' then
    raise exception 'kb_baseline_freeze: snapshot % is already %', p_snapshot, s.status;
  end if;
  if coalesce((s.harvest_state ->> 'done')::boolean, false) is not true then
    raise exception 'kb_baseline_freeze: harvest of % is not done', p_snapshot;
  end if;

  v_hash := public.kb_baseline_content_hash(p_snapshot);
  select max(t.version) into v_tax from public.product_taxonomy_versions t;

  v_counts := jsonb_build_object(
    'notices_per_query', (select jsonb_object_agg(q.q, q.n) from (
        select x.q, count(*) as n from public.kb_baseline_notices n, unnest(n.queries) x(q)
        where n.snapshot_id = p_snapshot group by x.q) q),
    'ted_totals_per_query', (select jsonb_object_agg(e.key, e.value -> 'total') from jsonb_each(s.harvest_state -> 'main') e),
    'kb_baseline_notices', (select jsonb_build_object('total', count(*),
        'competition', count(*) filter (where n.kind = 'competition'),
        'result', count(*) filter (where n.kind = 'result'),
        'modification', count(*) filter (where n.kind = 'modification'),
        'eforms', count(*) filter (where n.notice_identifier is not null))
        from public.kb_baseline_notices n where n.snapshot_id = p_snapshot),
    'kb_baseline_procedures', (select jsonb_build_object('total', sum(z.n), 'by_outcome', jsonb_object_agg(z.outcome, z.n))
        from (select p.outcome, count(*) as n from public.kb_baseline_procedures p where p.snapshot_id = p_snapshot group by p.outcome) z),
    'procedures_by_link_method', (select jsonb_object_agg(coalesce(z.link_method, 'none'), z.n)
        from (select p.link_method, count(*) as n from public.kb_baseline_procedures p where p.snapshot_id = p_snapshot group by p.link_method) z),
    'procedures_by_classification', (select jsonb_object_agg(coalesce(z.m, 'none'), z.n)
        from (select p.classification_method as m, count(*) as n from public.kb_baseline_procedures p where p.snapshot_id = p_snapshot group by 1) z),
    'kb_baseline_stats', (select jsonb_object_agg(z.scope_level, z.n)
        from (select t.scope_level, count(*) as n from public.kb_baseline_stats t where t.snapshot_id = p_snapshot group by 1) z),
    'kb_patterns', (select jsonb_object_agg(z.kind, z.n)
        from (select t.kind, count(*) as n from public.kb_patterns t where t.snapshot_id = p_snapshot group by 1) z),
    'kb_chunks', (select jsonb_object_agg(z.kind, z.n)
        from (select k.kind, count(*) as n from public.kb_chunks k where k.layer = 'baseline' and k.snapshot_id = p_snapshot group by 1) z),
    'ted_requests', s.harvest_state -> 'requests',
    'harvest_runs', s.harvest_state -> 'runs');

  v_manifest := jsonb_build_object(
    'snapshot_id', p_snapshot,
    'cutoff_date', s.cutoff_date,
    'rule', 'TED notices with PD <= cutoff; only procedures closed at the cutoff (tenders still open stay in ted_live)',
    'queries', s.queries,
    'counts', v_counts,
    'taxonomy_version', v_tax,
    'classifier', 'ontology rules-v1 (CPV + calibre + keyword stages) via the read-only port kb_baseline_classify; no LLM stage',
    'content_sha256', v_hash,
    'content_hash_method', 'sha256 of newline-joined "publication_number:md5(raw::text)" ordered by publication_number (collate C); kb_baseline_content_hash()',
    'harvest', jsonb_build_object('started_at', s.harvest_state -> 'started_at', 'finished_at', s.harvest_state -> 'finished_at',
                                  'edge_function', 'kb-baseline-harvest'),
    'file', null);

  if p_activate then
    update public.kb_snapshots set active = false where active and id <> p_snapshot;
  end if;

  update public.kb_snapshots set
    status = 'frozen',
    frozen_at = now(),
    active = p_activate,
    counts = v_counts,
    manifest = v_manifest,
    content_sha256 = v_hash,
    taxonomy_version = v_tax
  where id = p_snapshot;

  return v_manifest;
end
$$;
revoke all on function public.kb_baseline_freeze(text, boolean) from public, anon, authenticated;
grant execute on function public.kb_baseline_freeze(text, boolean) to service_role;
