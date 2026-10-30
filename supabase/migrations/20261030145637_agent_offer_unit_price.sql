-- WS4 refinement: WS5 counter-offers carry counter_unit_price instead of unit_price.
--   agent_message_append: quote/counter payloads get unit_price = counter_unit_price when only the latter is
--                         given, so agent_context (evaluate_quote) and the episode summary read one key.
--   agent_payload_summary: falls back to counter_unit_price.
-- CREATE OR REPLACE keeps the grants (service_role only).

-- One line describing an offer payload: "unit price 0.21 EUR × 1000000 rounds = 210000 EUR, lead time 90 days, DAP".
create or replace function public.agent_payload_summary(p_payload jsonb)
returns text
language sql
immutable
security definer
set search_path = ''
as $$
  select nullif(concat_ws(', ',
    case when coalesce(p_payload ->> 'unit_price', p_payload ->> 'counter_unit_price') is not null then
      'unit price ' || coalesce(p_payload ->> 'unit_price', p_payload ->> 'counter_unit_price')
      || coalesce(' ' || (p_payload ->> 'currency'), '')
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
    -- offers always carry unit_price (WS5 counters send counter_unit_price)
    if not v_payload ? 'unit_price' and v_payload ? 'counter_unit_price' then
      v_payload := v_payload || jsonb_build_object('unit_price', v_payload -> 'counter_unit_price');
    end if;
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
