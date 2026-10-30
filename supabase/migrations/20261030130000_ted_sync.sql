-- TED ingestion layer for the four focus CPV classes (knowledge_base/21-focus-scope.md).
-- Written by the ted-sync Edge Function with the service role; readable by anyone (public data).

create table if not exists public.ted_notices (
  publication_number text primary key,
  kind text not null check (kind in ('competition', 'result')),
  notice_type text,
  publication_date date,
  legal_basis text,                       -- 32009L0081 = defence directive, 32014L0024 = classic
  procedure_type text,
  title_en text,                          -- notice-title.eng ("Country – CPV label – title")
  title_original text,
  description text,
  original_language text,
  buyer_name text,
  buyer_identifier text,                  -- free text, e.g. "NIP 9521818707"
  buyer_country text,
  buyer_legal_type text,
  main_activity text,
  cpv_codes text[] not null default '{}',
  focus_classes text[] not null default '{}',  -- ammunition, firearms, parts, miscellaneous_weapons
  estimated_value numeric,
  estimated_currency text,
  deadline_tender timestamptz,            -- latest lot deadline
  deadline_request timestamptz,           -- request-to-participate deadline (restricted procedures)
  result_value numeric,
  result_currency text,
  bids_received numeric,
  subcontracting text,
  direct_award_justification text,
  place_of_performance text[] not null default '{}',
  ted_url text generated always as ('https://ted.europa.eu/en/notice/-/detail/' || publication_number) stored,
  raw jsonb not null,
  first_seen_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists ted_notices_kind_date on public.ted_notices (kind, publication_date desc);
create index if not exists ted_notices_focus on public.ted_notices using gin (focus_classes);
create index if not exists ted_notices_cpv on public.ted_notices using gin (cpv_codes);

-- One row per winner. notice_value is the notice total: lot-level pairing needs the eForms XML.
create table if not exists public.ted_awards (
  publication_number text not null references public.ted_notices (publication_number) on delete cascade,
  winner_key text not null,
  winner_name text not null,
  winner_identifier text,
  winner_country text,
  winner_size text,
  winner_owner_nationality text,
  decision_date date,
  notice_value numeric,
  currency text,
  co_winners int,
  updated_at timestamptz not null default now(),
  primary key (publication_number, winner_key)
);
create index if not exists ted_awards_winner on public.ted_awards (winner_identifier);

create table if not exists public.ted_sync_runs (
  id bigint generated always as identity primary key,
  mode text not null,
  started_at timestamptz not null,
  finished_at timestamptz not null default now(),
  fetched int not null default 0,
  notices_upserted int not null default 0,
  awards_upserted int not null default 0,
  done boolean,
  error text,
  details jsonb
);

create table if not exists public.ted_sync_state (
  key text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);

create or replace view public.ted_open_tenders with (security_invoker = on) as
select publication_number, notice_type, publication_date, legal_basis, procedure_type, title_en,
       title_original, buyer_name, buyer_country, cpv_codes, focus_classes, estimated_value,
       estimated_currency, deadline_tender, deadline_request, ted_url
from public.ted_notices
where kind = 'competition'
  and cardinality(focus_classes) > 0
  and greatest(coalesce(deadline_tender, '-infinity'), coalesce(deadline_request, '-infinity')) >= now();

-- Public read, no public writes (the service role bypasses RLS).
alter table public.ted_notices enable row level security;
alter table public.ted_awards enable row level security;
alter table public.ted_sync_runs enable row level security;
alter table public.ted_sync_state enable row level security;
drop policy if exists "public read" on public.ted_notices;
drop policy if exists "public read" on public.ted_awards;
drop policy if exists "public read" on public.ted_sync_runs;
create policy "public read" on public.ted_notices for select to anon, authenticated using (true);
create policy "public read" on public.ted_awards for select to anon, authenticated using (true);
create policy "public read" on public.ted_sync_runs for select to anon, authenticated using (true);
