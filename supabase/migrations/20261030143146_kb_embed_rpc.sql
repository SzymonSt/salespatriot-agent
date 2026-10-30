-- WS2 embeddings: queue/store RPCs, run log, status, pg_net trigger and cron for the Edge Function kb-embed.
-- Contract: docs/agent-context-rag-implementation.md §3.1, §3.3 and §5 WS2.
-- Model gemini-embedding-001, 768 dimensions, RETRIEVAL_DOCUMENT, L2-normalised; embedding_model = 'gemini-embedding-001@768'.
-- kb-embed also embeds public.agent_memory through WS4's memory_embed_queue / memory_embed_store.
--
-- One-off setup per project (not in git; the secret is generated inside the database and never printed):
--   select vault.create_secret('https://<project-ref>.supabase.co/functions/v1/kb-embed', 'kb_embed_url');
--   select vault.create_secret(replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''), 'kb_embed_secret');
-- The Edge Function reads GOOGLE_API_KEY from its own secrets (entered in the Supabase dashboard).
-- Free plan budget: at most 288 runs/day, and kb_embed_trigger() calls the function only while work is pending.

-- ---------------------------------------------------------------- run log (also the lease against overlapping runs)

create table public.kb_embed_runs (
  id bigint generated always as identity primary key,
  mode text not null,
  status text not null default 'running',     -- running | ok | rate_limited | error | abandoned
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  embedded int not null default 0,            -- kb_chunks rows stored
  memory_embedded int not null default 0,     -- agent_memory rows stored
  batches int not null default 0,
  remaining int,                              -- kb_chunks still pending after the run
  memory_remaining int,
  errors jsonb not null default '[]',
  details jsonb
);
alter table public.kb_embed_runs enable row level security;
create policy "public read" on public.kb_embed_runs for select to anon, authenticated using (true);
revoke insert, update, delete, truncate, references, trigger on public.kb_embed_runs from anon, authenticated;

-- ---------------------------------------------------------------- queue and store

-- Chunks without an embedding or whose content changed since embedding, oldest change first.
create or replace function public.kb_embed_queue(p_limit int default 100)
returns setof public.kb_chunks
language sql stable security definer set search_path = '' as $$
  select k.*
  from public.kb_chunks k
  where k.embedding is null or k.embedded_hash is distinct from k.content_hash
  order by k.updated_at, k.id
  limit greatest(1, least(coalesce(p_limit, 100), 500))
$$;

-- p_rows = [{id, embedding: [768 floats], model?, content_hash?}]. Writes only embedding, embedding_model,
-- embedded_hash and embedded_at (the baseline guard of WS8 allows exactly these on frozen rows).
-- A row whose content_hash no longer matches (content changed while embedding) is skipped.
create or replace function public.kb_embed_store(p_rows jsonb)
returns int
language plpgsql volatile security definer set search_path = '' as $$
declare
  v_n int;
begin
  if p_rows is null or jsonb_typeof(p_rows) <> 'array' then
    raise exception using errcode = '22023', message = 'kb_embed_store: p_rows must be a JSON array';
  end if;
  with r as (
    select (x ->> 'id')::uuid as id,
           (x ->> 'embedding')::extensions.vector(768) as embedding,
           coalesce(nullif(x ->> 'model', ''), 'gemini-embedding-001@768') as model,
           nullif(x ->> 'content_hash', '') as content_hash
    from jsonb_array_elements(p_rows) x
  ),
  u as (
    update public.kb_chunks k
       set embedding = r.embedding,
           embedding_model = r.model,
           embedded_hash = k.content_hash,
           embedded_at = now()
      from r
     where k.id = r.id
       and r.embedding is not null
       and (r.content_hash is null or r.content_hash = k.content_hash)
    returning k.id
  )
  select count(*)::int into v_n from u;
  return v_n;
end
$$;

-- ---------------------------------------------------------------- secret, lease, status

create or replace function public.kb_embed_check_secret(candidate text)
returns boolean
language sql stable security definer set search_path = '' as $$
  select candidate is not null and exists (
    select 1 from vault.decrypted_secrets where name = 'kb_embed_secret' and decrypted_secret = candidate
  )
$$;

-- Pending and embedded counts for kb_chunks and (when WS4's table exists) agent_memory.
create or replace function public.kb_embed_pending()
returns jsonb
language plpgsql stable security definer set search_path = '' as $$
declare
  v_kb jsonb;
  v_mem jsonb := null;
begin
  select jsonb_build_object(
           'total', count(*),
           'embedded', count(*) filter (where k.embedding is not null and k.embedded_hash is not distinct from k.content_hash),
           'pending', count(*) filter (where k.embedding is null or k.embedded_hash is distinct from k.content_hash))
    into v_kb
  from public.kb_chunks k;
  if to_regclass('public.agent_memory') is not null then
    begin
      execute $q$
        select jsonb_build_object(
                 'total', count(*),
                 'embedded', count(*) filter (where m.embedding is not null and m.embedded_hash is not distinct from m.content_hash),
                 'pending', count(*) filter (where (m.embedding is null or m.embedded_hash is distinct from m.content_hash)
                                               and m.status not in ('rejected', 'superseded')))
        from public.agent_memory m $q$ into v_mem;
    exception when others then
      v_mem := jsonb_build_object('error', sqlerrm);
    end;
  end if;
  return jsonb_build_object('kb_chunks', v_kb, 'agent_memory', v_mem);
end
$$;

-- Starts a run and returns its id, or null while another run is active (lease of 160 s, longer than one run).
create or replace function public.kb_embed_begin(p_mode text default 'manual')
returns bigint
language plpgsql volatile security definer set search_path = '' as $$
declare
  v_id bigint;
begin
  perform pg_advisory_xact_lock(hashtext('kb_embed_begin'));
  update public.kb_embed_runs
     set status = 'abandoned', finished_at = now()
   where status = 'running' and started_at < now() - interval '160 seconds';
  if exists (select 1 from public.kb_embed_runs where status = 'running') then
    return null;
  end if;
  insert into public.kb_embed_runs (mode) values (left(coalesce(nullif(btrim(p_mode), ''), 'manual'), 40))
  returning id into v_id;
  return v_id;
end
$$;

-- p_result = {status, embedded, memory_embedded, batches, errors: [...], details: {...}}.
create or replace function public.kb_embed_finish(p_id bigint, p_result jsonb)
returns jsonb
language plpgsql volatile security definer set search_path = '' as $$
declare
  v_pending jsonb := public.kb_embed_pending();
begin
  update public.kb_embed_runs
     set status = left(coalesce(nullif(p_result ->> 'status', ''), 'ok'), 40),
         finished_at = now(),
         embedded = coalesce((p_result ->> 'embedded')::int, 0),
         memory_embedded = coalesce((p_result ->> 'memory_embedded')::int, 0),
         batches = coalesce((p_result ->> 'batches')::int, 0),
         remaining = (v_pending -> 'kb_chunks' ->> 'pending')::int,
         memory_remaining = (v_pending -> 'agent_memory' ->> 'pending')::int,
         errors = case when jsonb_typeof(p_result -> 'errors') = 'array' then p_result -> 'errors' else '[]'::jsonb end,
         details = p_result -> 'details'
   where id = p_id;
  return v_pending;
end
$$;

create or replace function public.kb_embed_status()
returns jsonb
language sql stable security definer set search_path = '' as $$
  select public.kb_embed_pending() || jsonb_build_object(
    'model', 'gemini-embedding-001@768',
    'last_runs', coalesce((
      select jsonb_agg(to_jsonb(r) - 'details' order by r.id desc)
      from (select * from public.kb_embed_runs order by id desc limit 5) r), '[]'::jsonb))
$$;

-- ---------------------------------------------------------------- trigger and schedule

-- Calls the Edge Function from the database when work is pending (or always with p_force).
--   select public.kb_embed_trigger();                       -- what cron runs
--   select public.kb_embed_trigger('backfill', 100, true);  -- by hand
create or replace function public.kb_embed_trigger(p_mode text default 'cron', p_budget_seconds int default 100,
                                                   p_force boolean default false)
returns bigint
language plpgsql volatile security definer set search_path = '' as $$
declare
  v_pending boolean;
  v_url text;
  v_secret text;
begin
  v_pending := exists (select 1 from public.kb_chunks k
                       where k.embedding is null or k.embedded_hash is distinct from k.content_hash);
  if not v_pending and to_regclass('public.agent_memory') is not null then
    begin
      execute $q$ select exists (select 1 from public.agent_memory m
                                 where (m.embedding is null or m.embedded_hash is distinct from m.content_hash)
                                   and m.status not in ('rejected', 'superseded')) $q$ into v_pending;
    exception when others then
      v_pending := false;
    end;
  end if;
  if not v_pending and not coalesce(p_force, false) then
    return null;
  end if;

  select decrypted_secret into v_url from vault.decrypted_secrets where name = 'kb_embed_url';
  select decrypted_secret into v_secret from vault.decrypted_secrets where name = 'kb_embed_secret';
  if v_url is null or v_secret is null then
    raise warning 'kb_embed_trigger: vault secrets kb_embed_url / kb_embed_secret are missing';
    return null;
  end if;

  return net.http_post(
    url := v_url,
    headers := jsonb_build_object('Content-Type', 'application/json', 'x-sync-secret', v_secret),
    body := jsonb_build_object('mode', coalesce(p_mode, 'cron'),
                               'budgetSeconds', least(greatest(coalesce(p_budget_seconds, 100), 5), 120)),
    timeout_milliseconds := 140000
  );
end
$$;

-- ---------------------------------------------------------------- grants

do $$
declare f regprocedure;
begin
  for f in
    select p.oid::regprocedure
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.proname in ('kb_embed_queue', 'kb_embed_store', 'kb_embed_check_secret', 'kb_embed_pending',
                        'kb_embed_begin', 'kb_embed_finish', 'kb_embed_status', 'kb_embed_trigger')
  loop
    execute format('revoke all on function %s from public, anon, authenticated', f);
    execute format('grant execute on function %s to service_role', f);
  end loop;
end
$$;

select cron.schedule('kb-embed', '*/5 * * * *', $job$ select public.kb_embed_trigger(); $job$);
