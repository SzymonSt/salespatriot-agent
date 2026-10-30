-- Hourly TED sync: pg_cron schedules, pg_net calls the ted-sync Edge Function.
-- One-off setup per project (not in git; the secret is generated inside the database):
--   select vault.create_secret('https://<project-ref>.supabase.co/functions/v1/ted-sync', 'ted_sync_url');
--   select vault.create_secret(replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''), 'ted_sync_secret');
-- Free plan budget: 24 runs/day ≈ 720 Edge Function invocations/month of the 500,000 included.

create extension if not exists pg_cron;
create extension if not exists pg_net with schema extensions;

-- Used by the Edge Function (service_role only) to check the x-sync-secret header.
create or replace function public.ted_sync_check_secret(candidate text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from vault.decrypted_secrets
    where name = 'ted_sync_secret' and decrypted_secret = candidate
  );
$$;
revoke all on function public.ted_sync_check_secret(text) from public, anon, authenticated;
grant execute on function public.ted_sync_check_secret(text) to service_role;

-- Calls the Edge Function from the database. Also handy by hand:
--   select public.ted_sync_trigger('backfill');   select public.ted_sync_trigger('incremental', 7);
create or replace function public.ted_sync_trigger(mode text default 'incremental', days int default 2)
returns bigint
language sql
security definer
set search_path = ''
as $$
  select net.http_post(
    url := (select decrypted_secret from vault.decrypted_secrets where name = 'ted_sync_url'),
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'x-sync-secret', (select decrypted_secret from vault.decrypted_secrets where name = 'ted_sync_secret')
    ),
    body := jsonb_build_object('mode', mode, 'days', days),
    timeout_milliseconds := 140000
  );
$$;
revoke all on function public.ted_sync_trigger(text, int) from public, anon, authenticated;

select cron.schedule('ted-sync-hourly', '17 * * * *', $job$ select public.ted_sync_trigger('incremental', 2); $job$);
