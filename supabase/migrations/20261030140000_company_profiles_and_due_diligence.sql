-- Company profiles (suppliers, buyers) and supplier due diligence (score, KYC K1-K11, deal baseline).
-- Written by supabase_sync.py and supplier_due_diligence.py with the service-role key.
-- Joins to the TED sync tables: suppliers.identifier/country = ted_awards.winner_identifier/winner_country.

-- ---------------------------------------------------------------- suppliers & buyers

create table if not exists public.suppliers (
  id                      text primary key,             -- e.g. supplier-cze-65414128
  name                    text not null,                -- English display name
  legal_name              text,                         -- registered name (used for registers and sanctions)
  country                 text not null,                -- ISO 3166-1 alpha-3, as in TED
  city                    text,
  website                 text,
  identifier              text,                         -- national id / VAT from TED (winner-identifier)
  company_size            text,                         -- TED winner-size
  summary                 text,
  ted_activity            jsonb,
  track_record            jsonb,
  top_cpv                 jsonb,
  top_counterparts        jsonb,
  weapons_ammo_categories jsonb,
  capabilities            jsonb,
  offering                jsonb,
  catalogue               jsonb,
  recent_notices          jsonb,
  extra                   jsonb not null default '{}',  -- any profile keys without a column
  updated_at              timestamptz not null default now()
);
create index if not exists suppliers_identifier_idx on public.suppliers (identifier, country);

create table if not exists public.buyers (
  id                      text primary key,
  name                    text not null,
  legal_name              text,
  country                 text not null,
  city                    text,
  website                 text,
  identifier              text,
  legal_type              text,
  main_activity           text,
  summary                 text,
  ted_activity            jsonb,
  top_cpv                 jsonb,
  top_counterparts        jsonb,
  weapons_ammo_categories jsonb,
  recent_notices          jsonb,
  extra                   jsonb not null default '{}',
  updated_at              timestamptz not null default now()
);

-- One row per awarded lot won by a supplier (from TED award XML).
create table if not exists public.supplier_contracts (
  id                  bigint generated always as identity primary key,
  supplier_id         text not null references public.suppliers (id) on delete cascade,
  notice_number       text not null,
  lot_id              text,
  notice_type         text,
  published           date,
  notice_title        text,
  title               text,
  description         text,
  contract_title      text,
  buyer               text,
  buyer_country       text,
  cpv_codes           text[],
  quantity            text,
  duration            text,
  framework_agreement boolean,
  awarded_value       numeric,
  awarded_currency    text,
  tenders_received    integer,
  co_winners          jsonb,
  subcontracting      text,
  award_date          date,
  contract_signed     date,
  winner_confirmed    boolean,
  ted_url             text
);
create index if not exists supplier_contracts_supplier_idx on public.supplier_contracts (supplier_id);
create index if not exists supplier_contracts_notice_idx on public.supplier_contracts (notice_number);

-- ---------------------------------------------------------------- due diligence

create table if not exists public.supplier_due_diligence (
  supplier_id               text primary key references public.suppliers (id) on delete cascade,
  method_version            text not null,
  assessed_at               date not null,
  total_score               numeric(3,1) not null check (total_score between 1 and 10),
  score_before_caps         numeric(3,1),
  verdict                   text not null,
  confidence                text not null check (confidence in ('high', 'medium', 'low')),
  data_coverage             numeric(3,2),
  identity_score            numeric(3,1),     -- null = no open data for the category
  ownership_score           numeric(3,1),
  track_record_score        numeric(3,1),
  financial_score           numeric(3,1),
  defence_credentials_score numeric(3,1),
  integrity_score           numeric(3,1),
  red_flags                 text[] not null default '{}',
  caps_applied              text[] not null default '{}',
  gates                     jsonb not null default '[]',
  categories                jsonb not null,   -- per category: label, weight, score, reasoning, findings, red_flags, evidence
  supplier_documents        integer not null default 0,
  sanctions_lists_loaded    jsonb,
  disclaimer                text,
  updated_at                timestamptz not null default now()
);

create table if not exists public.supplier_kyc (
  supplier_id    text primary key references public.suppliers (id) on delete cascade,
  kyc_status     text not null check (kyc_status in ('verified', 'limited', 'blocked')),
  kyc_checked_at date not null,
  refresh_due    date not null,
  open_items     text[] not null default '{}',
  updated_at     timestamptz not null default now()
);

-- K1-K11, one row per check.
create table if not exists public.supplier_kyc_checks (
  supplier_id text not null references public.suppliers (id) on delete cascade,
  check_code  text not null check (check_code ~ '^K([1-9]|1[01])$'),
  title       text not null,
  gate        text not null,
  status      text not null check (status in ('pass', 'fail', 'review', 'needs_upload', 'pending_platform', 'info', 'not_checked')),
  summary     text,
  sources     text[] not null default '{}',
  detail      jsonb,
  checked_at  date not null,
  primary key (supplier_id, check_code)
);

create table if not exists public.supplier_deal_baseline (
  supplier_id            text primary key references public.suppliers (id) on delete cascade,
  largest_lot_eur        numeric,
  revenue_eur            numeric,
  revenue_year           integer,
  single_bid_share       numeric(3,2),
  trader_not_manufacturer boolean,
  related_supplier_ids   text[] not null default '{}',
  baseline               jsonb not null,   -- full D1/D6/D9/D10/D12/D13 block
  updated_at             timestamptz not null default now()
);

-- Documents uploaded by suppliers (licences, certificates, accounts, UBO declarations).
-- Replaces data/supplier_evidence/<id>.json. See that folder's README for types and points.
create table if not exists public.supplier_documents (
  id                  bigint generated always as identity primary key,
  supplier_id         text not null references public.suppliers (id) on delete cascade,
  type                text not null check (type in (
                        'arms_trade_licence', 'export_licence', 'facility_security_clearance', 'aqap', 'ncage',
                        'certider', 'nspa_source_file', 'iso_9001', 'en_9100', 'customer_reference',
                        'financial_statements', 'ubo_declaration')),
  title               text,
  issuer              text,
  number              text,
  valid_until         date,
  file                text,                 -- storage path
  verification_status text not null default 'declared' check (verification_status in ('declared', 'document_verified')),
  data                jsonb not null default '{}',  -- type-specific fields (revenue, owners, ...)
  uploaded_at         timestamptz not null default now()
);
create index if not exists supplier_documents_supplier_idx on public.supplier_documents (supplier_id);

-- ---------------------------------------------------------------- convenience view

create or replace view public.supplier_overview with (security_invoker = true) as
select s.id, s.name, s.legal_name, s.country, s.city, s.company_size,
       d.total_score, d.verdict, d.confidence,
       d.identity_score, d.ownership_score, d.track_record_score,
       d.financial_score, d.defence_credentials_score, d.integrity_score,
       cardinality(d.red_flags) as red_flag_count,
       k.kyc_status, k.refresh_due, cardinality(k.open_items) as open_kyc_items,
       d.assessed_at
from public.suppliers s
left join public.supplier_due_diligence d on d.supplier_id = s.id
left join public.supplier_kyc k on k.supplier_id = s.id;

-- ---------------------------------------------------------------- access
-- Public open-data tables: anyone with the anon key may read. Writes need the service role
-- (which bypasses RLS). Supplier documents are private: no anon policy at all.

alter table public.suppliers              enable row level security;
alter table public.buyers                 enable row level security;
alter table public.supplier_contracts     enable row level security;
alter table public.supplier_due_diligence enable row level security;
alter table public.supplier_kyc           enable row level security;
alter table public.supplier_kyc_checks    enable row level security;
alter table public.supplier_deal_baseline enable row level security;
alter table public.supplier_documents     enable row level security;

do $$
declare t text;
begin
  foreach t in array array['suppliers', 'buyers', 'supplier_contracts', 'supplier_due_diligence',
                           'supplier_kyc', 'supplier_kyc_checks', 'supplier_deal_baseline']
  loop
    execute format('drop policy if exists "public read" on public.%I', t);
    execute format('create policy "public read" on public.%I for select to anon, authenticated using (true)', t);
  end loop;
end $$;
