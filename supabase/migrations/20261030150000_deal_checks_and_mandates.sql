-- Per-deal due diligence (D1-D14) and agent mandates (K11). Written by deal_due_diligence.py --push.

create table if not exists public.deal_checks (
  deal_id            text primary key,          -- <publication_number>:<supplier id>
  publication_number text not null,             -- ted_open_tenders / ted_notices
  supplier_id        text not null references public.suppliers (id) on delete cascade,
  method_version     text not null,
  checked_at         date not null,
  decision           text not null check (decision in ('GO', 'GO WITH CONDITIONS', 'ESCALATE', 'NO-GO')),
  tender             jsonb not null,
  supplier           jsonb not null,
  blocking           text[] not null default '{}',   -- why ESCALATE / NO-GO
  conditions         text[] not null default '{}',   -- go into the contract draft
  updated_at         timestamptz not null default now()
);
create index if not exists deal_checks_tender_idx on public.deal_checks (publication_number);
create index if not exists deal_checks_supplier_idx on public.deal_checks (supplier_id);

create table if not exists public.deal_check_items (
  deal_id    text not null references public.deal_checks (deal_id) on delete cascade,
  code       text not null check (code ~ '^D([1-9]|1[0-4])$'),
  title      text not null,
  gate       text not null,
  status     text not null check (status in ('pass', 'condition', 'escalate', 'fail', 'info')),
  summary    text,
  evidence   text[] not null default '{}',
  conditions text[] not null default '{}',
  primary key (deal_id, code)
);

-- K11: what each company's agent may do on its own. company_id = supplier id, or 'buyer:<buyer name>'.
create table if not exists public.agent_mandates (
  id                       bigint generated always as identity primary key,
  company_id               text not null,
  active                   boolean not null default true,
  max_autonomous_value_eur numeric not null,
  min_supplier_score       numeric(3,1) not null default 6.0,
  allowed_kyc_status       text[] not null default '{verified,limited}',
  allowed_cpv_prefixes     text[] not null default '{3531,3532,3533,3534}',
  escalation_contact       text,
  signed_by                text,
  signed_at                timestamptz,
  created_at               timestamptz not null default now()
);
create unique index if not exists agent_mandates_active_idx on public.agent_mandates (company_id) where active;

alter table public.deal_checks      enable row level security;
alter table public.deal_check_items enable row level security;
alter table public.agent_mandates   enable row level security;

drop policy if exists "public read" on public.deal_checks;
create policy "public read" on public.deal_checks for select to anon, authenticated using (true);
drop policy if exists "public read" on public.deal_check_items;
create policy "public read" on public.deal_check_items for select to anon, authenticated using (true);
-- agent_mandates: no anon policy (private; service role only).
