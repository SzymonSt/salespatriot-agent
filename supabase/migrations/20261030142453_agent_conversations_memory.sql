-- WS4: agent conversations, messages, tool calls, deal outcomes and platform memory.
-- Contract: docs/agent-context-rag-implementation.md §3.3, §3.5 and §5 WS4.
--
-- Write path: service role only, through the RPCs below. anon may read demo conversations and their
-- messages (demo = true) so the UI can show the timeline via Realtime. agent_memory holds platform-layer
-- knowledge only (decision 8): episodes from our own conversations, preferences, distilled lessons.
--
-- Additive to §3.5: agent_conversations.calibre, .country, .updated_at (scope for priors and memory,
-- filled from the tender when null); deal_outcomes.unit_price, .quantity, .unit; agent_memory.conversation_id,
-- .embedded_hash, .embedded_at; memory_feedback(). Nothing in §3.5 is renamed or dropped.

create extension if not exists vector with schema extensions;

-- ------------------------------------------------------------------------------------------ tables

create table if not exists public.agent_conversations (
  id                 uuid primary key default gen_random_uuid(),
  publication_number text,
  buyer_id           text,
  supplier_id        text,
  family_id          text,
  calibre            text,
  country            char(3),
  status             text not null default 'open'
                     check (status in ('open', 'quoted', 'accepted', 'rejected', 'escalated', 'closed')),
  buyer_mandate      jsonb,
  supplier_mandate   jsonb,
  demo               boolean not null default true,
  outcome            jsonb,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  closed_at          timestamptz,
  constraint agent_conversations_closed_at check ((status = 'closed') = (closed_at is not null))
);
create index if not exists agent_conversations_publication on public.agent_conversations (publication_number);
create index if not exists agent_conversations_buyer on public.agent_conversations (buyer_id);
create index if not exists agent_conversations_supplier on public.agent_conversations (supplier_id);
create index if not exists agent_conversations_scope on public.agent_conversations (family_id, status);

create table if not exists public.agent_messages (
  id              uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references public.agent_conversations (id) on delete cascade,
  seq             int not null check (seq > 0),
  sender_role     text not null check (sender_role in ('buyer', 'supplier', 'system')),
  sender_id       text,
  type            text not null check (type in ('introduction', 'rfq', 'quote', 'counter', 'accept', 'reject',
                                                 'question', 'answer', 'reference_request', 'reference_answer',
                                                 'escalate', 'check')),
  text            text,
  payload         jsonb not null default '{}'::jsonb,
  refs            text[] not null default '{}',
  model           text,
  agent_version   text,
  created_at      timestamptz not null default now(),
  constraint agent_messages_conversation_seq unique (conversation_id, seq)
);

create table if not exists public.agent_tool_calls (
  id              bigint generated always as identity primary key,
  conversation_id uuid references public.agent_conversations (id) on delete cascade,
  message_id      uuid references public.agent_messages (id) on delete set null,
  tool            text not null,
  args            jsonb not null default '{}'::jsonb,
  result_refs     text[] not null default '{}',
  latency_ms      int,
  created_at      timestamptz not null default now()
);
create index if not exists agent_tool_calls_conversation on public.agent_tool_calls (conversation_id, created_at);
create index if not exists agent_tool_calls_message on public.agent_tool_calls (message_id);

create table if not exists public.deal_outcomes (
  conversation_id   uuid primary key references public.agent_conversations (id) on delete cascade,
  delivered_on_time boolean,
  quality           text,
  final_price       numeric,          -- agreed total price of the deal
  currency          text,
  unit_price        numeric,          -- agreed price per unit, when a quantity was stated
  quantity          numeric,
  unit              text,
  notes             text,
  reported_by       text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create table if not exists public.agent_memory (
  id              uuid primary key default gen_random_uuid(),
  kind            text not null check (kind in ('episode', 'lesson', 'preference', 'pattern_proposal')),
  role            text check (role in ('buyer', 'supplier')),
  family_id       text,
  country         char(3),
  counterparty    text,
  text            text not null check (length(btrim(text)) > 0),
  facts           jsonb not null default '{}'::jsonb,
  evidence_refs   text[] not null default '{}',
  support         int not null default 1 check (support >= 0),
  status          text not null default 'proposed'
                  check (status in ('proposed', 'validated', 'rejected', 'superseded')),
  visibility      text not null default 'parties' check (visibility in ('public', 'parties', 'owner')),
  owner_id        text,
  party_ids       text[] not null default '{}',
  conversation_id uuid references public.agent_conversations (id) on delete set null,
  content_hash    text generated always as (md5(text)) stored,
  embedded_hash   text,             -- content_hash at the time of embedding
  embedding       extensions.vector(768),
  embedding_model text,
  embedded_at     timestamptz,
  use_count       int not null default 0,
  helpful         int not null default 0,
  unhelpful       int not null default 0,
  expires_at      timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index if not exists agent_memory_status_family on public.agent_memory (status, family_id);
create index if not exists agent_memory_owner on public.agent_memory (owner_id);
create index if not exists agent_memory_parties on public.agent_memory using gin (party_ids);
create unique index if not exists agent_memory_episode_conversation
  on public.agent_memory (conversation_id) where kind = 'episode';
create index if not exists agent_memory_embedding
  on public.agent_memory using hnsw (embedding extensions.vector_cosine_ops);
create index if not exists agent_memory_pending on public.agent_memory (updated_at)
  where embedding is null or embedded_hash is distinct from content_hash;

-- ------------------------------------------------------------------------------------------ RLS

alter table public.agent_conversations enable row level security;
alter table public.agent_messages      enable row level security;
alter table public.agent_tool_calls    enable row level security;
alter table public.deal_outcomes       enable row level security;
alter table public.agent_memory        enable row level security;

revoke all on table public.agent_conversations, public.agent_messages, public.agent_tool_calls,
                    public.deal_outcomes, public.agent_memory from anon, authenticated;
grant select on table public.agent_conversations, public.agent_messages to anon, authenticated;

drop policy if exists "demo read" on public.agent_conversations;
create policy "demo read" on public.agent_conversations
  for select to anon, authenticated using (demo);

drop policy if exists "demo read" on public.agent_messages;
create policy "demo read" on public.agent_messages
  for select to anon, authenticated
  using (exists (select 1 from public.agent_conversations c where c.id = conversation_id and c.demo));
-- agent_tool_calls, deal_outcomes, agent_memory: no anon/authenticated policy (service role only).

-- Realtime: the UI subscribes to messages (and status changes) of demo conversations.
do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    if not exists (select 1 from pg_publication_tables
                   where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'agent_messages') then
      alter publication supabase_realtime add table public.agent_messages;
    end if;
    if not exists (select 1 from pg_publication_tables
                   where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'agent_conversations') then
      alter publication supabase_realtime add table public.agent_conversations;
    end if;
  end if;
end $$;

-- ------------------------------------------------------------------------------------------ helpers

-- Fill family, calibre and country from the tender when the caller leaves them null.
create or replace function public.agent_conversations_fill()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_family  text;
  v_calibre text;
  v_country text;
begin
  if tg_op = 'INSERT' and new.publication_number is not null then
    if new.family_id is null then
      select t.family_id, t.calibres[1]
        into v_family, v_calibre
      from public.ui_tender_families t
      where t.publication_number = new.publication_number
      order by t.in_focus desc nulls last, t.confidence desc nulls last, t.family_id
      limit 1;
      if v_family is null then
        -- not classified to a family yet: keep the class (same id space as product_families)
        select i.class_id into v_family
        from public.product_items i
        where i.publication_number = new.publication_number and i.side = 'demand' and i.class_id is not null
        order by i.confidence desc nulls last
        limit 1;
      end if;
      new.family_id := v_family;
      new.calibre := coalesce(new.calibre, v_calibre);
    end if;
    if new.country is null then
      select upper(btrim(n.buyer_country)) into v_country
      from public.ted_notices n
      where n.publication_number = new.publication_number;
      if length(v_country) = 3 then
        new.country := v_country;
      end if;
    end if;
  end if;
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists agent_conversations_fill on public.agent_conversations;
create trigger agent_conversations_fill
  before insert or update on public.agent_conversations
  for each row execute function public.agent_conversations_fill();

-- One line describing an offer payload: "unit price 0.21 EUR × 1000000 rounds = 210000 EUR, lead time 90 days, DAP".
create or replace function public.agent_payload_summary(p_payload jsonb)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select nullif(concat_ws(', ',
    case when p_payload ? 'unit_price' then
      'unit price ' || (p_payload ->> 'unit_price') || coalesce(' ' || (p_payload ->> 'currency'), '')
      || coalesce(' × ' || (p_payload ->> 'quantity') || coalesce(' ' || (p_payload ->> 'unit'), ''), '')
    end,
    case when coalesce(p_payload ->> 'total_price', p_payload ->> 'total') is not null then
      'total ' || coalesce(p_payload ->> 'total_price', p_payload ->> 'total') || coalesce(' ' || (p_payload ->> 'currency'), '')
    end,
    case when coalesce(p_payload ->> 'lead_time_days', p_payload ->> 'lead_time') is not null then
      'lead time ' || coalesce((p_payload ->> 'lead_time_days') || ' days', p_payload ->> 'lead_time')
    end,
    p_payload ->> 'incoterms',
    case when jsonb_typeof(p_payload -> 'conditions') = 'array' and jsonb_array_length(p_payload -> 'conditions') > 0 then
      'conditions: ' || (select string_agg(c, '; ') from jsonb_array_elements_text(p_payload -> 'conditions') c)
    end
  ), '')
  where p_payload is not null and jsonb_typeof(p_payload) = 'object';
$$;

-- jsonb array of strings → deduplicated text[] in first-seen order (null/empty → '{}').
create or replace function public.agent_text_array(p_value jsonb)
returns text[]
language sql
immutable
security definer
set search_path = ''
as $$
  select coalesce(array(
    select t.v
    from jsonb_array_elements_text(case when jsonb_typeof(p_value) = 'array' then p_value else '[]'::jsonb end)
         with ordinality as t(v, o)
    where nullif(btrim(t.v), '') is not null
    group by t.v
    order by min(t.o)), '{}'::text[]);
$$;

-- ------------------------------------------------------------------------------------------ messages

-- Append one message: assigns seq atomically (row lock on the conversation) and enforces the state machine.
--   rfq        only while status = open; sender buyer (or system)
--   quote      only after an rfq, while open/quoted/escalated; sender supplier (or system) → quoted
--   counter    only after an rfq, while open/quoted/escalated                                → quoted
--   accept     only after a quote/counter, while quoted/escalated; not by the party that made the last offer → accepted
--   reject     while open/quoted/escalated                                                   → rejected
--   escalate   while open/quoted/accepted/escalated                                          → escalated
--   introduction, question, answer, reference_request, reference_answer, check: any status except closed, no change
-- Closed conversations accept no messages. sender_id defaults to the conversation's party id and must match it.
create or replace function public.agent_message_append(p_conversation_id uuid, p_msg jsonb)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  c           public.agent_conversations%rowtype;
  v_type      text := lower(btrim(p_msg ->> 'type'));
  v_role      text := lower(btrim(p_msg ->> 'sender_role'));
  v_sender    text := nullif(btrim(p_msg ->> 'sender_id'), '');
  v_payload   jsonb := coalesce(p_msg -> 'payload', '{}'::jsonb);
  v_has_rfq   boolean;
  v_offer_by  text;
  v_seq       int;
  v_status    text;
  v_id        uuid;
  v_created   timestamptz;
begin
  if p_msg is null or jsonb_typeof(p_msg) <> 'object' then
    raise exception using errcode = '22023', message = 'agent_message_append: p_msg must be a JSON object';
  end if;
  if jsonb_typeof(v_payload) <> 'object' then
    raise exception using errcode = '22023', message = 'agent_message_append: payload must be a JSON object';
  end if;
  if p_msg ? 'refs' and jsonb_typeof(p_msg -> 'refs') not in ('array', 'null') then
    raise exception using errcode = '22023', message = 'agent_message_append: refs must be an array of strings';
  end if;
  if v_type is null or v_type not in ('introduction', 'rfq', 'quote', 'counter', 'accept', 'reject', 'question', 'answer',
                                      'reference_request', 'reference_answer', 'escalate', 'check') then
    raise exception using errcode = '22023', message = format('agent_message_append: unknown message type %L', v_type);
  end if;
  if v_role is null or v_role not in ('buyer', 'supplier', 'system') then
    raise exception using errcode = '22023', message = format('agent_message_append: unknown sender_role %L', v_role);
  end if;

  select * into c from public.agent_conversations where id = p_conversation_id for update;
  if not found then
    raise exception using errcode = 'P0002', message = format('conversation %s not found', p_conversation_id);
  end if;
  if c.status = 'closed' then
    raise exception using errcode = '22023', message = format('conversation %s is closed', c.id);
  end if;

  if v_role = 'buyer' then
    if v_sender is not null and c.buyer_id is not null and v_sender <> c.buyer_id then
      raise exception using errcode = '42501',
        message = format('sender %s is not the buyer of conversation %s', v_sender, c.id);
    end if;
    v_sender := coalesce(v_sender, c.buyer_id);
  elsif v_role = 'supplier' then
    if v_sender is not null and c.supplier_id is not null and v_sender <> c.supplier_id then
      raise exception using errcode = '42501',
        message = format('sender %s is not the supplier of conversation %s', v_sender, c.id);
    end if;
    v_sender := coalesce(v_sender, c.supplier_id);
  end if;

  select coalesce(bool_or(m.type = 'rfq'), false),
         (array_agg(m.sender_role order by m.seq desc) filter (where m.type in ('quote', 'counter')))[1],
         coalesce(max(m.seq), 0)
    into v_has_rfq, v_offer_by, v_seq
  from public.agent_messages m
  where m.conversation_id = c.id;

  v_status := c.status;
  if v_type = 'rfq' then
    if c.status <> 'open' then
      raise exception using errcode = '22023', message = format('illegal transition: rfq while %s', c.status);
    end if;
    if v_role = 'supplier' then
      raise exception using errcode = '22023', message = 'illegal transition: an rfq comes from the buyer';
    end if;
  elsif v_type in ('quote', 'counter') then
    if not v_has_rfq then
      raise exception using errcode = '22023', message = format('illegal transition: %s before an rfq', v_type);
    end if;
    if c.status not in ('open', 'quoted', 'escalated') then
      raise exception using errcode = '22023', message = format('illegal transition: %s while %s', v_type, c.status);
    end if;
    if v_type = 'quote' and v_role = 'buyer' then
      raise exception using errcode = '22023', message = 'illegal transition: a quote comes from the supplier';
    end if;
    v_status := 'quoted';
  elsif v_type = 'accept' then
    if v_offer_by is null then
      raise exception using errcode = '22023', message = 'illegal transition: accept before a quote or counter';
    end if;
    if c.status not in ('quoted', 'escalated') then
      raise exception using errcode = '22023', message = format('illegal transition: accept while %s', c.status);
    end if;
    if v_role <> 'system' and v_role = v_offer_by then
      raise exception using errcode = '22023', message = 'illegal transition: a party cannot accept its own offer';
    end if;
    v_status := 'accepted';
  elsif v_type = 'reject' then
    if c.status not in ('open', 'quoted', 'escalated') then
      raise exception using errcode = '22023', message = format('illegal transition: reject while %s', c.status);
    end if;
    v_status := 'rejected';
  elsif v_type = 'escalate' then
    if c.status not in ('open', 'quoted', 'accepted', 'escalated') then
      raise exception using errcode = '22023', message = format('illegal transition: escalate while %s', c.status);
    end if;
    v_status := 'escalated';
  end if;

  v_seq := v_seq + 1;
  insert into public.agent_messages (conversation_id, seq, sender_role, sender_id, type, text, payload, refs,
                                     model, agent_version)
  values (c.id, v_seq, v_role, v_sender, v_type, nullif(p_msg ->> 'text', ''), v_payload,
          public.agent_text_array(p_msg -> 'refs'),
          nullif(p_msg ->> 'model', ''), nullif(p_msg ->> 'agent_version', ''))
  returning id, created_at into v_id, v_created;

  update public.agent_conversations
     set status = v_status
   where id = c.id;

  return jsonb_build_object('id', v_id, 'conversation_id', c.id, 'seq', v_seq, 'type', v_type,
                            'sender_role', v_role, 'sender_id', v_sender, 'previous_status', c.status,
                            'status', v_status, 'created_at', v_created, 'ref', 'msg:' || v_id);
end;
$$;

-- ------------------------------------------------------------------------------------------ close

-- Close a conversation: status closed, closed_at, outcome (merged; final_status = status before closing),
-- deal_outcomes upsert when outcome fields are given, one episode memory per conversation (upserted, so a later
-- delivery report refines the same episode), then kb_refresh(array['conversation']) when WS1 has created it.
-- Calling it again on a closed conversation merges the new outcome fields (e.g. delivered_on_time) and refreshes
-- the episode.
create or replace function public.agent_conversation_close(p_id uuid, p_outcome jsonb default '{}'::jsonb)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  c            public.agent_conversations%rowtype;
  v_outcome    jsonb := coalesce(p_outcome, '{}'::jsonb);
  v_final      text;
  v_buyer      text;
  v_supplier   text;
  v_n_msgs     int;
  v_n_offers   int;
  v_n_escal    int;
  v_refs       text[];
  v_rfq        text;
  v_first      jsonb;
  v_last       jsonb;
  v_last_type  text;
  v_do         public.deal_outcomes%rowtype;
  v_has_do     boolean := false;
  v_text       text;
  v_facts      jsonb;
  v_episode    uuid;
  v_kb         jsonb;
  v_warnings   text[] := '{}';
begin
  if jsonb_typeof(v_outcome) <> 'object' then
    raise exception using errcode = '22023', message = 'agent_conversation_close: p_outcome must be a JSON object';
  end if;

  select * into c from public.agent_conversations where id = p_id for update;
  if not found then
    raise exception using errcode = 'P0002', message = format('conversation %s not found', p_id);
  end if;
  v_final := case when c.status = 'closed' then coalesce(c.outcome ->> 'final_status', 'closed') else c.status end;

  -- deal outcome (agreed price now, delivery and quality later)
  if v_outcome ?| array['delivered_on_time', 'quality', 'final_price', 'currency', 'unit_price', 'quantity', 'unit',
                        'notes', 'reported_by'] then
    insert into public.deal_outcomes as d (conversation_id, delivered_on_time, quality, final_price, currency,
                                           unit_price, quantity, unit, notes, reported_by)
    values (c.id,
            (v_outcome ->> 'delivered_on_time')::boolean,
            nullif(v_outcome ->> 'quality', ''),
            (v_outcome ->> 'final_price')::numeric,
            nullif(upper(btrim(v_outcome ->> 'currency')), ''),
            (v_outcome ->> 'unit_price')::numeric,
            (v_outcome ->> 'quantity')::numeric,
            nullif(v_outcome ->> 'unit', ''),
            nullif(v_outcome ->> 'notes', ''),
            nullif(v_outcome ->> 'reported_by', ''))
    on conflict (conversation_id) do update set
      delivered_on_time = coalesce(excluded.delivered_on_time, d.delivered_on_time),
      quality           = coalesce(excluded.quality, d.quality),
      final_price       = coalesce(excluded.final_price, d.final_price),
      currency          = coalesce(excluded.currency, d.currency),
      unit_price        = coalesce(excluded.unit_price, d.unit_price),
      quantity          = coalesce(excluded.quantity, d.quantity),
      unit              = coalesce(excluded.unit, d.unit),
      notes             = coalesce(excluded.notes, d.notes),
      reported_by       = coalesce(excluded.reported_by, d.reported_by),
      updated_at        = now();
  end if;
  select * into v_do from public.deal_outcomes where conversation_id = c.id;
  v_has_do := found;

  update public.agent_conversations
     set status    = 'closed',
         closed_at = coalesce(closed_at, now()),
         outcome   = coalesce(outcome, '{}'::jsonb) || v_outcome || jsonb_build_object('final_status', v_final)
   where id = c.id;

  -- episode summary from the messages
  select count(*)::int,
         (count(*) filter (where m.type in ('quote', 'counter')))::int,
         (count(*) filter (where m.type = 'escalate'))::int,
         (array_agg(coalesce(m.text, public.agent_payload_summary(m.payload)) order by m.seq)
            filter (where m.type = 'rfq'))[1],
         (array_agg(m.payload order by m.seq) filter (where m.type in ('quote', 'counter')))[1],
         (array_agg(m.payload order by m.seq desc) filter (where m.type in ('quote', 'counter')))[1],
         (array_agg(m.type order by m.seq desc) filter (where m.type in ('quote', 'counter')))[1]
    into v_n_msgs, v_n_offers, v_n_escal, v_rfq, v_first, v_last, v_last_type
  from public.agent_messages m
  where m.conversation_id = c.id;

  select public.agent_text_array(to_jsonb(array['conv:' || c.id::text]
                                          || case when c.publication_number is not null
                                                  then array['ted:' || c.publication_number] else '{}'::text[] end
                                          || coalesce(array_agg(r.ref order by r.seq, r.o), '{}'::text[])))
    into v_refs
  from (select m.seq, u.ref, u.o
        from public.agent_messages m
        cross join lateral unnest(m.refs) with ordinality as u(ref, o)
        where m.conversation_id = c.id) r;

  select coalesce(s.name, c.supplier_id) into v_supplier from (select 1) x
    left join public.suppliers s on s.id = c.supplier_id;
  select coalesce(b.name, n.buyer_name, c.buyer_id) into v_buyer from (select 1) x
    left join public.buyers b on b.id = c.buyer_id
    left join public.ted_notices n on n.publication_number = c.publication_number;

  v_text := left(concat_ws(E'\n',
    format('DEAL %s · buyer %s ↔ supplier %s · %s%s%s',
           v_final, coalesce(v_buyer, 'unknown'), coalesce(v_supplier, 'unknown'),
           coalesce(c.family_id, 'family unknown'), coalesce(' ' || c.calibre, ''),
           coalesce(' · tender ' || c.publication_number || coalesce(' (' || c.country || ')', ''), '')),
    'Asked: ' || left(v_rfq, 300),
    'Quoted: ' || public.agent_payload_summary(v_first),
    case when v_n_offers > 1 then 'Final ' || v_last_type || ': ' || public.agent_payload_summary(v_last) end,
    format('Rounds: %s offer(s), %s escalation(s), %s message(s)', v_n_offers, v_n_escal, v_n_msgs),
    case when v_has_do then
      'Outcome: ' || concat_ws(', ',
        'final price ' || (v_do.final_price::text || coalesce(' ' || v_do.currency, '')),
        'unit price ' || (v_do.unit_price::text || coalesce(' ' || v_do.currency, '')),
        'delivered on time ' || v_do.delivered_on_time::text,
        'quality ' || v_do.quality,
        left(v_do.notes, 300))
    end), 2000);

  v_facts := jsonb_strip_nulls(jsonb_build_object(
    'conversation_id', c.id, 'publication_number', c.publication_number,
    'buyer_id', c.buyer_id, 'supplier_id', c.supplier_id, 'family_id', c.family_id, 'calibre', c.calibre,
    'country', c.country, 'final_status', v_final, 'demo', c.demo,
    'messages', v_n_msgs, 'offers', v_n_offers, 'escalations', v_n_escal,
    'first_offer', v_first, 'last_offer', v_last,
    'final_price', v_do.final_price, 'unit_price', v_do.unit_price, 'currency', v_do.currency,
    'quantity', v_do.quantity, 'delivered_on_time', v_do.delivered_on_time, 'quality', v_do.quality));

  insert into public.agent_memory (kind, role, family_id, country, counterparty, text, facts, evidence_refs,
                                   support, status, visibility, owner_id, party_ids, conversation_id)
  values ('episode', null, c.family_id, c.country, null, v_text, v_facts, v_refs,
          1, 'validated', 'parties', null, array_remove(array[c.buyer_id, c.supplier_id], null), c.id)
  on conflict (conversation_id) where kind = 'episode' do update set
    family_id     = excluded.family_id,
    country       = excluded.country,
    text          = excluded.text,
    facts         = excluded.facts,
    evidence_refs = excluded.evidence_refs,
    party_ids     = excluded.party_ids,
    updated_at    = now()
  returning id into v_episode;

  -- conversation chunk for the RAG (WS1). Optional until kb_refresh exists; never fails the close.
  if to_regprocedure('public.kb_refresh(text[], timestamp with time zone)') is not null then
    begin
      execute 'select to_jsonb(public.kb_refresh($1))' into v_kb using array['conversation']::text[];
    exception when others then
      v_warnings := v_warnings || ('kb_refresh failed: ' || sqlerrm);
    end;
  else
    v_warnings := v_warnings || 'kb_refresh() not available yet: conversation chunk not built'::text;
  end if;

  return jsonb_build_object('conversation_id', c.id, 'status', 'closed', 'final_status', v_final,
                            'episode_id', v_episode, 'episode_ref', 'memory:' || v_episode,
                            'deal_outcome', v_has_do, 'kb_refresh', v_kb, 'warnings', to_jsonb(v_warnings));
end;
$$;

-- ------------------------------------------------------------------------------------------ memory

-- Insert (or, with "id" and writer = job, update) one memory entry. Returns its id.
--   writer = 'agent' (default): kind episode | preference | pattern_proposal, status proposed only, no updates.
--   writer = 'job' (distiller, close, human review run by the service role): any kind and status.
-- Rules: lessons cite at least one platform ref (conv:… or memory:…) — agent_memory holds platform knowledge only;
-- parties visibility needs party_ids or owner_id; owner visibility needs owner_id. An identical active entry
-- (same kind, text, family, owner and parties) is not duplicated: its support grows and its refs are merged.
create or replace function public.memory_write(p_entry jsonb)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  e            jsonb := p_entry;
  v_writer     text := coalesce(lower(btrim(p_entry ->> 'writer')), 'agent');
  v_id         uuid;
  v_kind       text := lower(btrim(p_entry ->> 'kind'));
  v_status     text := coalesce(lower(btrim(p_entry ->> 'status')), 'proposed');
  v_visibility text;
  v_role       text := nullif(lower(btrim(p_entry ->> 'role')), '');
  v_country    text := nullif(upper(btrim(p_entry ->> 'country')), '');
  v_text       text := btrim(p_entry ->> 'text');
  v_facts      jsonb := coalesce(p_entry -> 'facts', '{}'::jsonb);
  v_refs       text[] := public.agent_text_array(p_entry -> 'evidence_refs');
  v_parties    text[] := public.agent_text_array(p_entry -> 'party_ids');
  v_owner      text := nullif(btrim(p_entry ->> 'owner_id'), '');
  v_conv       uuid := nullif(p_entry ->> 'conversation_id', '')::uuid;
  v_support    int := coalesce((p_entry ->> 'support')::int, 1);
  v_embedding  extensions.vector(768);
  v_model      text := nullif(p_entry ->> 'embedding_model', '');
begin
  if e is null or jsonb_typeof(e) <> 'object' then
    raise exception using errcode = '22023', message = 'memory_write: entry must be a JSON object';
  end if;
  if v_writer not in ('agent', 'job') then
    raise exception using errcode = '22023', message = format('memory_write: unknown writer %L', v_writer);
  end if;

  -- update of an existing entry (status change, validation, merge) by a job
  if e ? 'id' then
    if v_writer <> 'job' then
      raise exception using errcode = '42501', message = 'memory_write: agents cannot update memory entries';
    end if;
    v_id := (e ->> 'id')::uuid;
    update public.agent_memory m set
      status        = coalesce(nullif(lower(btrim(e ->> 'status')), ''), m.status),
      visibility    = coalesce(nullif(lower(btrim(e ->> 'visibility')), ''), m.visibility),
      text          = coalesce(nullif(btrim(e ->> 'text'), ''), m.text),
      facts         = case when e ? 'facts' then m.facts || v_facts else m.facts end,
      evidence_refs = case when e ? 'evidence_refs'
                           then public.agent_text_array(to_jsonb(m.evidence_refs || v_refs)) else m.evidence_refs end,
      support       = coalesce((e ->> 'support')::int, m.support),
      party_ids     = case when e ? 'party_ids' then v_parties else m.party_ids end,
      expires_at    = case when e ? 'expires_at' then (e ->> 'expires_at')::timestamptz else m.expires_at end,
      updated_at    = now()
    where m.id = v_id;
    if not found then
      raise exception using errcode = 'P0002', message = format('memory_write: entry %s not found', v_id);
    end if;
    return v_id;
  end if;

  if v_kind is null or v_kind not in ('episode', 'lesson', 'preference', 'pattern_proposal') then
    raise exception using errcode = '22023', message = format('memory_write: unknown kind %L', v_kind);
  end if;
  if v_status not in ('proposed', 'validated', 'rejected', 'superseded') then
    raise exception using errcode = '22023', message = format('memory_write: unknown status %L', v_status);
  end if;
  if v_writer = 'agent' then
    if v_kind not in ('episode', 'preference', 'pattern_proposal') then
      raise exception using errcode = '42501', message = format('memory_write: agents cannot write %s entries', v_kind);
    end if;
    if v_status <> 'proposed' then
      raise exception using errcode = '42501', message = 'memory_write: agents write status proposed only';
    end if;
  end if;
  if v_text is null or v_text = '' then
    raise exception using errcode = '22023', message = 'memory_write: text is required';
  end if;
  if length(v_text) > 4000 then
    raise exception using errcode = '22023', message = 'memory_write: text longer than 4000 characters';
  end if;
  if jsonb_typeof(v_facts) <> 'object' then
    raise exception using errcode = '22023', message = 'memory_write: facts must be a JSON object';
  end if;
  if v_role is not null and v_role not in ('buyer', 'supplier') then
    raise exception using errcode = '22023', message = format('memory_write: unknown role %L', v_role);
  end if;
  if v_country is not null and v_country !~ '^[A-Z]{3}$' then
    raise exception using errcode = '22023', message = format('memory_write: country must be ISO alpha-3, got %L', v_country);
  end if;

  v_visibility := coalesce(nullif(lower(btrim(e ->> 'visibility')), ''),
                           case v_kind when 'lesson' then 'public' when 'episode' then 'parties' else 'owner' end);
  if v_visibility not in ('public', 'parties', 'owner') then
    raise exception using errcode = '22023', message = format('memory_write: unknown visibility %L', v_visibility);
  end if;
  if v_visibility = 'owner' and v_owner is null then
    raise exception using errcode = '22023', message = 'memory_write: owner visibility needs owner_id';
  end if;
  if v_visibility = 'parties' and v_owner is null and cardinality(v_parties) = 0 then
    raise exception using errcode = '22023', message = 'memory_write: parties visibility needs party_ids or owner_id';
  end if;
  if v_kind = 'lesson'
     and not exists (select 1 from unnest(v_refs) r where r like 'conv:%' or r like 'memory:%') then
    raise exception using errcode = '22023',
      message = 'memory_write: a lesson must cite at least one platform ref (conv:<id> or memory:<id>)';
  end if;
  if v_kind = 'episode' and v_conv is null then
    raise exception using errcode = '22023', message = 'memory_write: an episode needs conversation_id';
  end if;
  if e ? 'embedding' and jsonb_typeof(e -> 'embedding') = 'array' then
    v_embedding := (e ->> 'embedding')::extensions.vector(768);
  end if;

  -- one episode per conversation: a job refreshes it, an agent only adds refs to it
  if v_kind = 'episode' then
    select m.id into v_id from public.agent_memory m where m.kind = 'episode' and m.conversation_id = v_conv;
    if v_id is not null then
      update public.agent_memory m
         set text          = case when v_writer = 'job' then v_text else m.text end,
             facts         = case when v_writer = 'job' then m.facts || v_facts else m.facts end,
             evidence_refs = public.agent_text_array(to_jsonb(m.evidence_refs || v_refs)),
             updated_at    = now()
       where m.id = v_id;
      return v_id;
    end if;
  else
    -- same active entry already there: grow support, merge refs
    select m.id into v_id
    from public.agent_memory m
    where m.kind = v_kind
      and m.content_hash = md5(v_text)
      and m.family_id is not distinct from nullif(e ->> 'family_id', '')
      and m.owner_id is not distinct from v_owner
      and m.party_ids = v_parties
      and m.status not in ('rejected', 'superseded')
    limit 1;
    if v_id is not null then
      update public.agent_memory m
         set support       = m.support + greatest(v_support, 1),
             evidence_refs = public.agent_text_array(to_jsonb(m.evidence_refs || v_refs)),
             updated_at    = now()
       where m.id = v_id;
      return v_id;
    end if;
  end if;

  insert into public.agent_memory (kind, role, family_id, country, counterparty, text, facts, evidence_refs, support,
                                   status, visibility, owner_id, party_ids, conversation_id, expires_at,
                                   embedding, embedding_model, embedded_hash, embedded_at)
  values (v_kind, v_role, nullif(e ->> 'family_id', ''), v_country, nullif(btrim(e ->> 'counterparty'), ''),
          v_text, v_facts, v_refs, v_support, v_status, v_visibility, v_owner, v_parties, v_conv,
          (e ->> 'expires_at')::timestamptz,
          v_embedding, case when v_embedding is not null then coalesce(v_model, 'gemini-embedding-001@768') end,
          case when v_embedding is not null then md5(v_text) end,
          case when v_embedding is not null then now() end)
  returning id into v_id;
  return v_id;
end;
$$;

-- Recall memory for an agent. Candidates: validated public entries (lessons) + episodes/preferences/lessons the
-- viewer owns or is a party of. Expired, rejected, superseded and pattern proposals are never returned.
-- score = similarity × status weight (validated 1.0, proposed 0.5) × recency exp(-age_days/180)
--         × (helpful+1)/(helpful+unhelpful+2) × scope boost (family +0.5, country +0.25, counterparty +0.5)
-- similarity = 1 - cosine distance with a query embedding; 0.5 for rows not embedded yet; 1 without a query.
-- Family filter: the entry has no family, the same family, or the same class. Increments use_count of returned rows.
create or replace function public.memory_recall(
  p_query_embedding extensions.vector(768) default null,
  p_role            text default null,
  p_family_id       text default null,
  p_country         text default null,
  p_counterparty    text default null,
  p_viewer          text default null,
  p_limit           int default 5)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_limit  int := greatest(1, least(coalesce(p_limit, 5), 50));
  v_class  text;
  v_result jsonb;
begin
  if p_family_id is not null then
    select coalesce(f.parent_id, f.id) into v_class from public.product_families f where f.id = p_family_id;
  end if;

  with cand as (
    select m.*,
           case when p_query_embedding is null then 1.0
                when m.embedding is null then 0.5
                else greatest(0.0, 1.0 - (m.embedding operator(extensions.<=>) p_query_embedding)) end as sim,
           (p_family_id is not null and m.family_id = p_family_id) as fam_hit,
           (p_country is not null and m.country = upper(p_country)) as country_hit,
           (p_counterparty is not null and (m.counterparty = p_counterparty or p_counterparty = any (m.party_ids)))
             as cp_hit
    from public.agent_memory m
    left join public.product_families mf on mf.id = m.family_id
    where m.status in ('validated', 'proposed')
      and m.kind <> 'pattern_proposal'
      and (m.expires_at is null or m.expires_at > now())
      and ((m.visibility = 'public' and m.status = 'validated')
           or (p_viewer is not null and (m.owner_id = p_viewer
                                          or (m.visibility = 'parties' and p_viewer = any (m.party_ids)))))
      and (p_role is null or m.role is null or m.role = p_role)
      and (p_family_id is null or m.family_id is null or m.family_id = p_family_id
           or m.family_id = v_class or coalesce(mf.parent_id, mf.id) = v_class)
  ),
  scored as (
    select c.*,
           c.sim
           * case c.status when 'validated' then 1.0 else 0.5 end
           * exp(-extract(epoch from (now() - c.created_at)) / 86400.0 / 180.0)
           * (c.helpful + 1.0) / (c.helpful + c.unhelpful + 2.0)
           * (1.0 + case when c.fam_hit then 0.5 else 0 end
                  + case when c.country_hit then 0.25 else 0 end
                  + case when c.cp_hit then 0.5 else 0 end) as score
    from cand c
    order by score desc, c.created_at desc
    limit v_limit
  ),
  bump as (
    update public.agent_memory m set use_count = m.use_count + 1
    from scored s where m.id = s.id
    returning m.id
  )
  select coalesce(jsonb_agg(jsonb_build_object(
           'ref', 'memory:' || s.id, 'id', s.id, 'kind', s.kind, 'layer', 'platform',
           'role', s.role, 'family_id', s.family_id, 'country', s.country, 'counterparty', s.counterparty,
           'text', left(s.text, 300), 'support', s.support, 'status', s.status, 'visibility', s.visibility,
           'evidence_refs', to_jsonb(s.evidence_refs[1:8]), 'helpful', s.helpful, 'unhelpful', s.unhelpful,
           'created_at', s.created_at, 'score', round(s.score::numeric, 4))
         order by s.score desc, s.created_at desc), '[]'::jsonb)
    into v_result
  from scored s;   -- bump (data-modifying CTE) always runs to completion

  return v_result;
end;
$$;

-- Helpful / unhelpful feedback on a recalled entry (POST /v1/memory/{id}/feedback).
create or replace function public.memory_feedback(p_id uuid, p_helpful boolean)
returns jsonb
language sql
security definer
set search_path = ''
as $$
  update public.agent_memory m
     set helpful   = m.helpful + case when p_helpful then 1 else 0 end,
         unhelpful = m.unhelpful + case when p_helpful then 0 else 1 end
   where m.id = p_id
  returning jsonb_build_object('ref', 'memory:' || m.id, 'helpful', m.helpful, 'unhelpful', m.unhelpful);
$$;

-- Embedding queue for the kb-embed Edge Function (same pattern as kb_embed_queue / kb_embed_store).
create or replace function public.memory_embed_queue(p_limit int default 100)
returns setof public.agent_memory
language sql
stable
security definer
set search_path = ''
as $$
  select m.*
  from public.agent_memory m
  where (m.embedding is null or m.embedded_hash is distinct from m.content_hash)
    and m.status not in ('rejected', 'superseded')
  order by m.updated_at
  limit greatest(1, least(coalesce(p_limit, 100), 500));
$$;

-- p_rows = [{id, embedding: [768 floats, L2-normalised], model, content_hash?}]. A row whose content_hash no
-- longer matches (text changed after it was queued) is skipped and stays in the queue. Returns rows stored.
create or replace function public.memory_embed_store(p_rows jsonb)
returns int
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_n int;
begin
  if p_rows is null or jsonb_typeof(p_rows) <> 'array' then
    raise exception using errcode = '22023', message = 'memory_embed_store: p_rows must be a JSON array';
  end if;
  with r as (
    select (x ->> 'id')::uuid as id,
           (x ->> 'embedding')::extensions.vector(768) as embedding,
           coalesce(nullif(x ->> 'model', ''), 'gemini-embedding-001@768') as model,
           nullif(x ->> 'content_hash', '') as content_hash
    from jsonb_array_elements(p_rows) x
  ), u as (
    update public.agent_memory m
       set embedding = r.embedding,
           embedding_model = r.model,
           embedded_hash = m.content_hash,
           embedded_at = now()
      from r
     where m.id = r.id
       and (r.content_hash is null or r.content_hash = m.content_hash)
    returning m.id
  )
  select count(*)::int into v_n from u;
  return v_n;
end;
$$;

-- ------------------------------------------------------------------------------------------ grants

revoke all on function public.agent_conversations_fill() from public, anon, authenticated;
revoke all on function public.agent_payload_summary(jsonb) from public, anon, authenticated;
revoke all on function public.agent_text_array(jsonb) from public, anon, authenticated;
revoke all on function public.agent_message_append(uuid, jsonb) from public, anon, authenticated;
revoke all on function public.agent_conversation_close(uuid, jsonb) from public, anon, authenticated;
revoke all on function public.memory_write(jsonb) from public, anon, authenticated;
revoke all on function public.memory_recall(extensions.vector, text, text, text, text, text, int)
  from public, anon, authenticated;
revoke all on function public.memory_feedback(uuid, boolean) from public, anon, authenticated;
revoke all on function public.memory_embed_queue(int) from public, anon, authenticated;
revoke all on function public.memory_embed_store(jsonb) from public, anon, authenticated;

grant execute on function public.agent_payload_summary(jsonb) to service_role;
grant execute on function public.agent_text_array(jsonb) to service_role;
grant execute on function public.agent_message_append(uuid, jsonb) to service_role;
grant execute on function public.agent_conversation_close(uuid, jsonb) to service_role;
grant execute on function public.memory_write(jsonb) to service_role;
grant execute on function public.memory_recall(extensions.vector, text, text, text, text, text, int) to service_role;
grant execute on function public.memory_feedback(uuid, boolean) to service_role;
grant execute on function public.memory_embed_queue(int) to service_role;
grant execute on function public.memory_embed_store(jsonb) to service_role;

comment on table public.agent_conversations is
  'Buyer ⇄ supplier agent conversations (WS4). Write via agent_message_append / agent_conversation_close. anon reads demo rows only.';
comment on table public.agent_messages is
  'Messages of agent conversations; seq per conversation assigned by agent_message_append. Realtime publication.';
comment on table public.agent_memory is
  'Platform-layer memory only (episodes, preferences, lessons, pattern proposals). Write via memory_write, read via memory_recall.';
comment on table public.deal_outcomes is
  'Outcome of a closed conversation: agreed price (final_price = total, unit_price per unit), delivery, quality.';
