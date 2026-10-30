-- Value fallbacks (lots, framework maximum), one "next deadline" column, and a restart flag for backfills.

alter table public.ted_notices add column if not exists estimated_value_source text;  -- procedure | lots | framework_max

-- Restricted / negotiated procedures publish only the request-to-participate deadline; the tender
-- deadline comes later in the invitation, not on TED. next_deadline shows whichever applies.
create or replace view public.ted_open_tenders with (security_invoker = on) as
select publication_number, notice_type, publication_date, legal_basis, procedure_type, title_en,
       title_original, buyer_name, buyer_country, cpv_codes, focus_classes, estimated_value,
       estimated_currency, deadline_tender, deadline_request, ted_url,
       estimated_value_source,
       coalesce(deadline_tender, deadline_request) as next_deadline,
       case when deadline_tender is not null then 'tenders' else 'requests_to_participate' end as deadline_kind
from public.ted_notices
where kind = 'competition'
  and cardinality(focus_classes) > 0
  and greatest(coalesce(deadline_tender, '-infinity'), coalesce(deadline_request, '-infinity')) >= now();

drop function if exists public.ted_sync_trigger(text, int);
create or replace function public.ted_sync_trigger(mode text default 'incremental', days int default 2, restart boolean default false)
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
    body := jsonb_build_object('mode', mode, 'days', days, 'restart', restart),
    timeout_milliseconds := 140000
  );
$$;
revoke all on function public.ted_sync_trigger(text, int, boolean) from public, anon, authenticated;
