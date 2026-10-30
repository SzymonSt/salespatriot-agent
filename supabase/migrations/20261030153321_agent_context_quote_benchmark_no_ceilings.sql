-- evaluate_quote no longer benchmarks a quote total against framework ceilings.
--
-- Benchmark c) of agent_context compared the quote total with awarded lot values of supplier_contracts. 219 of the
-- 372 contract anchors are framework agreements, whose value is the framework ceiling (often shared by co-winners),
-- and 18 are below 100 (a unit price typed as the lot value). Those are now left out (with a warning), and one total
-- repeated on several lots or winners of a notice counts once. priors.baseline.unit_price_ref is added to refs.
-- Everything else is the same text as 20261030145357_agent_context_refine.sql. CREATE OR REPLACE keeps the grants.

create or replace function public.agent_context(
  p_role            text,
  p_purpose         text,
  p_subject         jsonb,
  p_query_embedding extensions.vector(768) default null,
  p_query_text      text default null,
  p_viewer          text default null,
  p_limit           int default 8)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_t0          timestamptz := clock_timestamp();
  v_role        text := lower(btrim(p_role));
  v_purpose     text := lower(btrim(p_purpose));
  v_subject     jsonb := coalesce(p_subject, '{}'::jsonb);
  v_lim         int := greatest(1, least(coalesce(p_limit, 8), 25));
  v_query       text := nullif(btrim(p_query_text), '');
  v_viewer      text := nullif(btrim(p_viewer), '');
  v_pub         text;
  v_supplier    text;
  v_buyer       text;
  v_buyer_name  text;
  v_buyer_ident text;
  v_family      text;
  v_class       text;
  v_calibre     text;
  v_country     text;
  v_scope_fam   text;
  v_parent      text;
  v_known       boolean;
  v_conv        public.agent_conversations%rowtype;
  v_msg         public.agent_messages%rowtype;
  v_t           jsonb;
  v_x           jsonb;
  v_sup         jsonb;
  v_dc          jsonb;
  f_tender      jsonb;
  f_self        jsonb;
  f_cp          jsonb;
  f_conv        jsonb;
  f_quote       jsonb;
  f_deal        jsonb;
  f_mandate     jsonb;
  v_candidates  jsonb := '[]'::jsonb;
  v_evidence    jsonb := '[]'::jsonb;
  v_anchors     jsonb := '[]'::jsonb;
  v_reg         jsonb := '{}'::jsonb;
  v_memory      jsonb := '[]'::jsonb;
  v_priors      jsonb := jsonb_build_object('scope', '{}'::jsonb, 'baseline', null, 'platform', null, 'blend', null);
  v_counterparty text;
  v_kinds       text[];
  v_do_evidence boolean := false;
  v_versions    jsonb;
  v_refs        jsonb;
  w             text[] := '{}';
  -- quote evaluation
  q_unit        numeric;
  q_cur         text;
  q_qty         numeric;
  q_total       numeric;
  q_cmp         numeric;
  q_pos         text;
  b_basis       text;
  b_n           int;
  b_p25         numeric;
  b_p50         numeric;
  b_p75         numeric;
  b_excl        int;
  v_limit_eur   numeric;
  v_largest_eur numeric;
begin
  if v_role is null or v_role not in ('buyer', 'supplier') then
    raise exception using errcode = '22023', message = format('agent_context: unknown role %L (buyer | supplier)', p_role);
  end if;
  if v_purpose is null or v_purpose not in ('assess_tender', 'find_suppliers', 'evaluate_quote', 'check_counterparty', 'search') then
    raise exception using errcode = '22023', message = format(
      'agent_context: unknown purpose %L (assess_tender | find_suppliers | evaluate_quote | check_counterparty | search)', p_purpose);
  end if;
  if jsonb_typeof(v_subject) <> 'object' then
    raise exception using errcode = '22023', message = 'agent_context: p_subject must be a JSON object';
  end if;

  v_pub      := nullif(btrim(v_subject ->> 'publication_number'), '');
  v_supplier := nullif(btrim(v_subject ->> 'supplier_id'), '');
  v_buyer    := nullif(btrim(v_subject ->> 'buyer_id'), '');
  v_family   := nullif(btrim(v_subject ->> 'family_id'), '');
  v_calibre  := nullif(btrim(v_subject ->> 'calibre'), '');
  v_country  := upper(nullif(btrim(v_subject ->> 'country'), ''));

  -- 1. conversation and message (evaluate_quote) ------------------------------------------------------------
  if v_purpose = 'evaluate_quote' then
    if nullif(v_subject ->> 'conversation_id', '') is null then
      raise exception using errcode = '22023', message = 'agent_context: evaluate_quote needs subject.conversation_id';
    end if;
    select * into v_conv from public.agent_conversations c where c.id = (v_subject ->> 'conversation_id')::uuid;
    if not found then
      raise exception using errcode = 'P0002', message = format('conversation %s not found', v_subject ->> 'conversation_id');
    end if;
    -- the viewer must be the party of p_role in this conversation (mandates and memory are per side)
    if v_viewer is not null
       and v_viewer is distinct from (case v_role when 'buyer' then v_conv.buyer_id else v_conv.supplier_id end) then
      raise exception using errcode = '42501',
        message = format('viewer %s is not the %s of conversation %s', v_viewer, v_role, v_conv.id);
    end if;
    select * into v_msg
    from public.agent_messages m
    where m.conversation_id = v_conv.id
      and case when nullif(v_subject ->> 'message_id', '') is not null
               then m.id = (v_subject ->> 'message_id')::uuid
               else m.type in ('quote', 'counter') end
    order by (m.sender_role <> v_role) desc, m.seq desc   -- default: the counterparty's latest offer
    limit 1;
    if v_msg.id is null then
      w := w || 'no quote or counter found for this subject: facts.quote unknown'::text;
    end if;
    v_pub      := coalesce(v_pub, v_conv.publication_number);
    v_supplier := coalesce(v_conv.supplier_id, v_supplier);
    v_buyer    := coalesce(v_conv.buyer_id, v_buyer);
    v_family   := coalesce(v_family, v_conv.family_id);
    v_calibre  := coalesce(v_calibre, v_conv.calibre);
    v_country  := coalesce(v_country, v_conv.country);
    f_conv := jsonb_build_object(
      'ref', 'conv:' || v_conv.id, 'layer', 'platform', 'conversation_id', v_conv.id, 'status', v_conv.status,
      'publication_number', v_conv.publication_number, 'buyer_id', v_conv.buyer_id, 'supplier_id', v_conv.supplier_id,
      'family_id', v_conv.family_id, 'calibre', v_conv.calibre, 'country', v_conv.country, 'demo', v_conv.demo,
      'messages', (select count(*) from public.agent_messages m where m.conversation_id = v_conv.id),
      'offers', (select count(*) from public.agent_messages m where m.conversation_id = v_conv.id and m.type in ('quote', 'counter')),
      'created_at', v_conv.created_at);
  end if;

  -- 2. tender ------------------------------------------------------------------------------------------------
  if v_pub is not null and v_purpose in ('assess_tender', 'find_suppliers', 'evaluate_quote') then
    v_t := public.agent_ctx_tender(v_pub, v_lim);
    f_tender := v_t -> 'tender';
    w := w || public.agent_text_array(v_t -> 'warnings');
    v_family      := coalesce(v_family, v_t -> 'scope' ->> 'family_id');
    v_class       := v_t -> 'scope' ->> 'class_id';
    v_calibre     := coalesce(v_calibre, v_t -> 'scope' ->> 'calibre');
    v_country     := coalesce(v_country, v_t -> 'scope' ->> 'country');
    v_buyer       := coalesce(v_buyer, v_t -> 'scope' ->> 'buyer_id');
    v_buyer_name  := v_t -> 'scope' ->> 'buyer_name';
    v_buyer_ident := v_t -> 'scope' ->> 'buyer_identifier';
  elsif v_purpose = 'assess_tender' then
    w := w || 'subject.publication_number missing: tender facts unknown'::text;
  end if;

  -- 3. scope: family vs class, normalised calibre --------------------------------------------------------------
  if v_family is not null then
    select f.parent_id, true into v_parent, v_known from public.product_families f where f.id = v_family;
    if not coalesce(v_known, false) then
      w := w || format('unknown family_id %s', v_family);
    elsif v_parent is null then
      v_class := coalesce(v_class, v_family);
      v_family := null;
    else
      v_class := coalesce(v_class, v_parent);
    end if;
  end if;
  if v_calibre is not null then
    v_calibre := coalesce(public.product_normalise_calibre(v_calibre), v_calibre);
  end if;
  v_scope_fam := coalesce(v_family, v_class);

  -- 4. purpose sections --------------------------------------------------------------------------------------
  if v_purpose = 'assess_tender' then
    if v_supplier is null then
      w := w || 'subject.supplier_id missing: facts.self unknown'::text;
    else
      v_sup := public.agent_ctx_supplier(v_supplier, v_scope_fam, v_calibre, v_lim);
      if v_sup is null then
        w := w || format('supplier %s has no profile: facts.self unknown', v_supplier);
      else
        f_self := jsonb_build_object(
          'ref', v_sup -> 'ref', 'supplier_id', v_supplier, 'name', v_sup -> 'name', 'country', v_sup -> 'country',
          'family_id', v_scope_fam,
          'proven_lots', v_sup -> 'in_scope' -> 'proven_lots', 'declared_items', v_sup -> 'in_scope' -> 'declared_items',
          'calibres', v_sup -> 'in_scope' -> 'calibres', 'calibre_match', v_sup -> 'in_scope' -> 'calibre_match',
          'best_evidence', v_sup -> 'in_scope' -> 'best_evidence', 'last_award', v_sup -> 'in_scope' -> 'last_award',
          'dd_score', v_sup -> 'dd_score', 'kyc_score', v_sup -> 'kyc_score', 'verdict', v_sup -> 'verdict',
          'kyc_status', v_sup -> 'kyc_status', 'red_flag_count', v_sup -> 'red_flag_count');
        if v_scope_fam is not null and coalesce((v_sup -> 'in_scope' ->> 'proven_lots')::int, 0) = 0 then
          w := w || case when coalesce((v_sup -> 'in_scope' ->> 'declared_items')::int, 0) = 0
                         then format('supplier has no TED-proven lot and no catalogue item in %s', v_scope_fam)
                         else format('supplier has no TED-proven lot in %s (declared catalogue items only)', v_scope_fam) end;
        end if;
      end if;
    end if;
    f_cp := public.agent_ctx_buyer(v_buyer, v_buyer_name, v_buyer_ident, v_pub, v_lim);
    v_counterparty := coalesce(v_buyer, 'buyer:' || v_buyer_name);
    v_kinds := array['award', 'contract', 'tender'];
    v_do_evidence := true;
    v_x := public.agent_ctx_deal_check(v_pub, v_supplier, v_lim);
    f_deal := v_x -> 'check';
    if v_x ->> 'warning' is not null then w := w || (v_x ->> 'warning'); end if;
    f_mandate := public.agent_ctx_mandate(case when v_viewer is null or v_viewer = v_supplier
                                               then array[v_supplier] else array[v_viewer] end);

  elsif v_purpose = 'find_suppliers' then
    if v_pub is not null then
      select coalesce(jsonb_agg(jsonb_build_object(
               'ref', case when m.supplier_id is not null then 'supplier:' || m.supplier_id
                           else 'org:' || coalesce(public.ui_name_key(m.org_ref), m.org_ref) end,
               'supplier_id', m.supplier_id, 'name', m.org_name, 'profiled', m.supplier_id is not null,
               'family_id', m.family_id, 'family_name', m.family_name, 'calibre', m.calibre,
               'key_matches', m.key_matches, 'proven_lots', m.proven_lots, 'declared_items', m.declared_items,
               'best_evidence', m.best_evidence, 'reasons', to_jsonb(m.reasons[1:v_lim]),
               'country', o.country, 'company_size', o.company_size, 'dd_score', o.total_score, 'verdict', o.verdict,
               'dd_confidence', o.confidence, 'red_flag_count', o.red_flag_count, 'kyc_status', o.kyc_status,
               'kyc_refresh_due', o.refresh_due, 'open_kyc_items', o.open_kyc_items, 'deal_check', null)
             order by m.ordinality), '[]'::jsonb)
        into v_candidates
      from public.ui_match_tender(v_pub, v_lim) with ordinality as m
      left join public.supplier_overview o on o.id = m.supplier_id;
    elsif v_scope_fam is not null then
      select coalesce(jsonb_agg(jsonb_build_object(
               'ref', 'supplier:' || s.supplier_id, 'supplier_id', s.supplier_id, 'name', o.name, 'profiled', true,
               'family_id', s.family_id, 'family_name', fam.name,
               'calibre', case when s.calibre_match then v_calibre end, 'key_matches', null,
               'proven_lots', s.proven_lots, 'declared_items', s.declared_items, 'best_evidence', s.best_evidence,
               'reasons', to_jsonb(array_remove(array[
                  case when v_family is not null then 'Same family: ' || coalesce(sf.name, v_family)
                       else 'Same class: ' || coalesce(cl.name, v_class) end,
                  case when v_calibre is not null then
                    case when s.calibre_match then 'Calibre ' || v_calibre || ' matches'
                         else 'Calibre ' || v_calibre || ' not stated on this company''s items' end end,
                  case when s.proven_lots > 0 then s.proven_lots || ' TED contract(s) in this '
                                                   || case when v_family is not null then 'family' else 'class' end end,
                  case when s.declared_items > 0 then 'Catalogue lists ' || s.declared_items || ' item(s)' end,
                  case when s.last_award is not null then 'Last TED contract ' || to_char(s.last_award, 'YYYY-MM-DD') end,
                  case when o.kyc_status is not null then 'KYC ' || o.kyc_status end], null)),
               'country', o.country, 'company_size', o.company_size, 'dd_score', o.total_score, 'verdict', o.verdict,
               'dd_confidence', o.confidence, 'red_flag_count', o.red_flag_count, 'kyc_status', o.kyc_status,
               'kyc_refresh_due', o.refresh_due, 'open_kyc_items', o.open_kyc_items, 'deal_check', null)
             order by s.calibre_match desc, s.proven_lots desc, s.declared_items desc, o.total_score desc nulls last,
                      s.supplier_id), '[]'::jsonb)
        into v_candidates
      from (select u.supplier_id,
                   (array_agg(u.family_id order by u.proven_lots desc, u.declared_items desc, u.family_id))[1] as family_id,
                   sum(u.proven_lots)::int as proven_lots, sum(u.declared_items)::int as declared_items,
                   max(u.last_award) as last_award,
                   coalesce(bool_or(v_calibre = any (u.calibres)), false) as calibre_match,
                   (array_agg(u.best_evidence order by case u.best_evidence
                      when 'independently_verified' then 0 when 'document_verified' then 1 when 'peer_attested' then 2
                      when 'declared' then 3 when 'inferred' then 4 else 5 end))[1] as best_evidence
            from public.ui_supplier_families u
            where (v_family is not null and u.family_id = v_family) or (v_family is null and u.class_id = v_class)
            group by u.supplier_id
            order by coalesce(bool_or(v_calibre = any (u.calibres)), false) desc, sum(u.proven_lots) desc,
                     sum(u.declared_items) desc, u.supplier_id
            limit v_lim) s
      left join public.supplier_overview o on o.id = s.supplier_id
      left join public.product_families fam on fam.id = s.family_id
      left join public.product_families sf on sf.id = v_family
      left join public.product_families cl on cl.id = v_class;
    else
      w := w || 'find_suppliers needs subject.publication_number or subject.family_id: no candidates'::text;
    end if;

    -- deal check decision per candidate (when deal_checks exists)
    if v_pub is not null and jsonb_array_length(v_candidates) > 0 then
      begin
        select jsonb_object_agg(d.supplier_id, jsonb_build_object('ref', 'deal:' || d.deal_id, 'decision', d.decision,
                                                                  'blocking', cardinality(d.blocking), 'checked_at', d.checked_at))
          into v_dc
        from public.deal_checks d where d.publication_number = v_pub;
      exception when undefined_table or undefined_column then
        v_dc := null;
        w := w || 'deal_checks not available yet (run deal_due_diligence.py --push): candidates[].deal_check unknown'::text;
      end;
      if v_dc is not null then
        select jsonb_agg(c.x || jsonb_build_object('deal_check', v_dc -> (c.x ->> 'supplier_id')) order by c.o)
          into v_candidates
        from jsonb_array_elements(v_candidates) with ordinality as c(x, o);
      end if;
    end if;

    if jsonb_array_length(v_candidates) = 0 then
      w := w || 'no matching suppliers found'::text;
    elsif exists (select 1 from jsonb_array_elements(v_candidates) c where c.value ->> 'supplier_id' is null) then
      w := w || format('%s of %s candidates have no KYC or due-diligence profile (dd_score unknown, not zero)',
                       (select count(*) from jsonb_array_elements(v_candidates) c where c.value ->> 'supplier_id' is null),
                       jsonb_array_length(v_candidates));
    end if;
    f_self := public.agent_ctx_buyer(coalesce(v_buyer, case when v_viewer like 'buyer-%' then v_viewer end),
                                     v_buyer_name, v_buyer_ident, v_pub, v_lim);
    v_kinds := array['contract', 'award'];
    v_do_evidence := v_scope_fam is not null or v_query is not null or p_query_embedding is not null;
    f_mandate := public.agent_ctx_mandate(case when v_viewer is null or v_viewer = v_buyer
                                               then array[v_buyer, 'buyer:' || v_buyer_name] else array[v_viewer] end);

  elsif v_purpose = 'evaluate_quote' then
    if v_role = 'buyer' then
      v_sup := public.agent_ctx_supplier(v_supplier, v_scope_fam, v_calibre, v_lim);
      f_cp := v_sup;
      v_counterparty := v_supplier;
      if v_sup is null then
        w := w || format('supplier %s has no profile: KYC and due diligence unknown', coalesce(v_supplier, '(none)'));
      end if;
      f_mandate := case when jsonb_typeof(v_conv.buyer_mandate) = 'object'
                        then v_conv.buyer_mandate || jsonb_build_object(
                               'ref', 'conv:' || v_conv.id || '#buyer_mandate',
                               'source', coalesce(v_conv.buyer_mandate ->> 'source', 'conversation mandate'))
                        else public.agent_ctx_mandate(array[v_buyer, 'buyer:' || v_buyer_name]) end;
    else
      f_cp := public.agent_ctx_buyer(v_buyer, v_buyer_name, v_buyer_ident, v_pub, v_lim);
      v_counterparty := v_buyer;
      f_mandate := case when jsonb_typeof(v_conv.supplier_mandate) = 'object'
                        then v_conv.supplier_mandate || jsonb_build_object(
                               'ref', 'conv:' || v_conv.id || '#supplier_mandate',
                               'source', coalesce(v_conv.supplier_mandate ->> 'source', 'conversation mandate'))
                        else public.agent_ctx_mandate(array[v_supplier]) end;
    end if;
    v_x := public.agent_ctx_deal_check(v_pub, v_supplier, v_lim);
    f_deal := v_x -> 'check';
    if v_x ->> 'warning' is not null then w := w || (v_x ->> 'warning'); end if;
    v_do_evidence := v_query is not null or p_query_embedding is not null;
    v_kinds := array['contract', 'award', 'conversation'];

  elsif v_purpose = 'check_counterparty' then
    if v_supplier is not null then
      f_cp := public.agent_ctx_supplier(v_supplier, v_scope_fam, v_calibre, v_lim);
      v_counterparty := v_supplier;
    elsif v_buyer is not null then
      f_cp := public.agent_ctx_buyer(v_buyer, null, null, null, v_lim);
      v_counterparty := v_buyer;
    else
      raise exception using errcode = '22023',
        message = 'agent_context: check_counterparty needs subject.supplier_id or subject.buyer_id';
    end if;
    if f_cp is null then
      w := w || format('no profile for %s: counterparty facts unknown', v_counterparty);
    end if;
    v_x := public.agent_ctx_company_chunks(v_supplier, case when v_supplier is null then v_buyer end, v_viewer, v_lim);
    v_evidence := v_x -> 'items';
    if v_x ->> 'warning' is not null then w := w || (v_x ->> 'warning'); end if;

  elsif v_purpose = 'search' then
    v_kinds := nullif(public.agent_text_array(v_subject -> 'kinds'), '{}'::text[]);
    v_do_evidence := true;
    if v_query is null and p_query_embedding is null then
      w := w || 'search without query text or embedding: newest matching chunks only'::text;
    end if;
  end if;

  -- 5. evidence (kb_search) ----------------------------------------------------------------------------------
  if v_do_evidence then
    v_x := public.agent_ctx_evidence(
      p_query_embedding, v_query, v_kinds, v_scope_fam, v_calibre,
      case when v_purpose = 'search' then nullif(btrim(v_subject ->> 'cpv_prefix'), '') end,
      case when v_purpose = 'search' then v_country end,
      v_viewer, v_lim,
      case when v_pub is not null then array['ted:' || v_pub] else '{}'::text[] end);
    v_evidence := v_x -> 'items';
    if v_x ->> 'warning' is not null then w := w || (v_x ->> 'warning'); end if;
  end if;

  -- 6. price anchors, regulatory, priors -----------------------------------------------------------------------
  if v_purpose in ('assess_tender', 'evaluate_quote') then
    v_x := public.agent_ctx_anchors(v_family, v_calibre, v_lim);
    v_anchors := v_x -> 'items';
    w := w || public.agent_text_array(v_x -> 'warnings');
  end if;
  if v_purpose in ('assess_tender', 'find_suppliers', 'evaluate_quote') and v_scope_fam is not null then
    v_reg := public.agent_ctx_regulatory(v_scope_fam, v_calibre, v_lim);
    v_x := public.agent_ctx_priors(v_family, v_class, v_calibre, v_country, v_lim);
    v_priors := v_x -> 'priors';
    w := w || public.agent_text_array(v_x -> 'warnings');
  end if;

  -- 7. quote evaluation ----------------------------------------------------------------------------------------
  if v_purpose = 'evaluate_quote' and v_msg.id is not null then
    q_unit := public.agent_ctx_num(v_msg.payload -> 'unit_price');
    q_cur := upper(nullif(btrim(v_msg.payload ->> 'currency'), ''));
    q_qty := public.agent_ctx_num(v_msg.payload -> 'quantity');
    q_total := coalesce(public.agent_ctx_num(v_msg.payload -> 'total_price'), public.agent_ctx_num(v_msg.payload -> 'total'),
                        q_unit * q_qty);
    if q_unit is null then w := w || 'quote unit price not stated (unknown)'::text; end if;
    if q_cur is null then w := w || 'quote currency not stated (unknown)'::text; end if;

    -- a) unit prices among the anchors, same currency
    if q_unit is not null and q_cur is not null then
      select count(*)::int, percentile_cont(0.25) within group (order by s.u), percentile_cont(0.5) within group (order by s.u),
             percentile_cont(0.75) within group (order by s.u)
        into b_n, b_p25, b_p50, b_p75
      from (select (a ->> 'unit_price')::numeric as u from jsonb_array_elements(v_anchors) a
            where a ->> 'unit_price' is not null and upper(a ->> 'currency') = q_cur) s;
      if b_n >= 3 then
        b_basis := 'anchor_unit_prices';
        q_cmp := q_unit;
      -- b) baseline unit-price quartiles (blended median), same currency
      elsif v_priors -> 'baseline' ->> 'unit_price_p25' is not null
            and upper(v_priors -> 'baseline' ->> 'currency') = q_cur then
        b_basis := 'baseline_unit_price';
        b_n := (v_priors -> 'baseline' ->> 'n_unit_prices')::int;
        b_p25 := (v_priors -> 'baseline' ->> 'unit_price_p25')::numeric;
        b_p50 := coalesce((v_priors -> 'blend' ->> 'unit_price_p50')::numeric, (v_priors -> 'baseline' ->> 'unit_price_p50')::numeric);
        b_p75 := (v_priors -> 'baseline' ->> 'unit_price_p75')::numeric;
        q_cmp := q_unit;
      end if;
    end if;
    -- c) awarded lot values against the quote total (indicative: lot quantities unknown). Left out: framework
    --    agreements (the value is the framework ceiling, often shared by co-winners), values below 100 (a unit price
    --    typed as the lot value) and the same total repeated on several lots or winners of one notice.
    if b_basis is null and q_total is not null and q_cur is not null then
      select count(*)::int, percentile_cont(0.25) within group (order by s.v), percentile_cont(0.5) within group (order by s.v),
             percentile_cont(0.75) within group (order by s.v)
        into b_n, b_p25, b_p50, b_p75
      from (select distinct a ->> 'publication_number' as pub, (a ->> 'value')::numeric as v
            from jsonb_array_elements(v_anchors) a
            where a ->> 'kind' = 'contract' and a ->> 'value' is not null and upper(a ->> 'currency') = q_cur
              and not coalesce((a ->> 'framework_agreement')::boolean, false)
              and (a ->> 'value')::numeric >= 100) s;
      select count(*)::int into b_excl
      from jsonb_array_elements(v_anchors) a
      where a ->> 'kind' = 'contract' and a ->> 'value' is not null and upper(a ->> 'currency') = q_cur
        and (coalesce((a ->> 'framework_agreement')::boolean, false) or (a ->> 'value')::numeric < 100);
      if b_excl > 0 then
        w := w || format('%s awarded lot value(s) in %s left out of the quote benchmark (framework ceilings or values '
                         'below 100, not lot prices)', b_excl, q_cur);
      end if;
      if b_n >= 3 then
        b_basis := 'awarded_lot_values';
        q_cmp := q_total;
        w := w || 'quote total compared with awarded lot values (lot quantities unknown): indicative only'::text;
      end if;
    end if;
    if b_basis is not null then
      q_pos := case when q_cmp < b_p25 then 'below_p25' when q_cmp > b_p75 then 'above_p75' else 'within_p25_p75' end;
      if q_pos <> 'within_p25_p75' then
        w := w || format('quote %s %s is %s of the anchors (p25 %s, p75 %s, n %s, basis %s)', round(q_cmp, 4), q_cur,
                         replace(q_pos, '_', ' '), round(b_p25, 4), round(b_p75, 4), b_n, b_basis);
      end if;
    elsif q_unit is not null or q_total is not null then
      w := w || format('quote not benchmarked: fewer than 3 comparable anchors in %s', coalesce(q_cur, 'the quote currency'));
    end if;

    -- capacity and mandate limits are in EUR
    v_largest_eur := (v_sup -> 'deal_baseline' ->> 'largest_lot_eur')::numeric;
    v_limit_eur := (f_mandate ->> 'max_autonomous_value_eur')::numeric;
    if q_total is not null and q_cur = 'EUR' then
      if v_largest_eur is not null and q_total > v_largest_eur then
        w := w || format('quote total EUR %s exceeds the supplier''s largest TED lot (EUR %s): check capacity (D6)',
                         round(q_total), round(v_largest_eur));
      end if;
      if v_limit_eur is not null and q_total > v_limit_eur then
        w := w || format('quote total EUR %s exceeds the mandate''s autonomous limit (EUR %s): escalate',
                         round(q_total), round(v_limit_eur));
      end if;
    elsif q_total is not null and q_cur is not null then
      w := w || format('quote in %s: mandate and capacity limits are in EUR (not converted)', q_cur);
    end if;

    f_quote := jsonb_build_object(
      'ref', 'msg:' || v_msg.id, 'layer', 'platform', 'message_id', v_msg.id, 'seq', v_msg.seq, 'type', v_msg.type,
      'sender_role', v_msg.sender_role, 'sender_id', v_msg.sender_id, 'text', left(v_msg.text, 300),
      'unit_price', q_unit, 'currency', q_cur, 'quantity', q_qty, 'unit', v_msg.payload ->> 'unit',
      'total_price', q_total,
      'lead_time_days', public.agent_ctx_num(v_msg.payload -> 'lead_time_days'),
      'incoterms', v_msg.payload ->> 'incoterms',
      'conditions', public.agent_ctx_slice(v_msg.payload -> 'conditions', v_lim),
      'demo_fixture', (v_msg.payload ->> 'demo_fixture')::boolean,
      'refs', to_jsonb(v_msg.refs[1:v_lim]),
      'benchmark', case when b_basis is not null then jsonb_build_object(
                     'basis', b_basis, 'n', b_n, 'p25', round(b_p25, 4), 'p50', round(b_p50, 4), 'p75', round(b_p75, 4),
                     'compared_value', q_cmp, 'currency', q_cur, 'position', q_pos) end);
  end if;

  -- 8. memory --------------------------------------------------------------------------------------------------
  if v_purpose <> 'search' then
    v_memory := public.memory_recall(p_query_embedding, v_role, v_scope_fam, v_country, v_counterparty, v_viewer, v_lim);
  end if;

  -- 9. versions and refs ---------------------------------------------------------------------------------------
  v_versions := jsonb_build_object(
    'taxonomy', (select max(t.version) from public.product_taxonomy_versions t),
    'kb', null, 'baseline', null, 'context', 'v1');
  begin
    v_versions := v_versions || jsonb_build_object(
      'kb', (select max(k.updated_at) from public.kb_chunks k where k.layer <> 'baseline'));
  exception when undefined_table or undefined_column then
    null;
  end;
  begin
    v_versions := v_versions || jsonb_build_object(
      'baseline', (select s.id from public.kb_snapshots s where s.active order by s.id limit 1));
  exception when undefined_table or undefined_column then
    null;
  end;

  v_refs := to_jsonb(public.agent_text_array(
      jsonb_build_array(f_tender -> 'ref', f_conv -> 'ref', f_quote -> 'ref', f_self -> 'ref', f_cp -> 'ref',
                        f_deal -> 'ref', v_reg -> 'ref', v_priors -> 'baseline' -> 'ref',
                        v_priors -> 'baseline' -> 'unit_price_ref')
      || coalesce((select jsonb_agg(x -> 'ref') from jsonb_array_elements(v_candidates) x), '[]'::jsonb)
      || coalesce((select jsonb_agg(x -> 'ref') from jsonb_array_elements(v_evidence) x), '[]'::jsonb)
      || coalesce((select jsonb_agg(x -> 'ref') from jsonb_array_elements(v_anchors) x), '[]'::jsonb)
      || coalesce((select jsonb_agg(x -> 'ref') from jsonb_array_elements(v_memory) x), '[]'::jsonb)));

  return jsonb_build_object(
    'purpose', v_purpose,
    'role', v_role,
    'viewer', v_viewer,
    'subject', v_subject,
    'scope', jsonb_build_object('publication_number', v_pub, 'supplier_id', v_supplier, 'buyer_id', v_buyer,
                                'family_id', v_family, 'class_id', v_class, 'calibre', v_calibre, 'country', v_country),
    'facts', jsonb_build_object('tender', f_tender, 'self', f_self, 'counterparty', f_cp, 'conversation', f_conv,
                                'quote', f_quote, 'deal_checks', f_deal, 'mandate', f_mandate),
    'candidates', v_candidates,
    'evidence', v_evidence,
    'price_anchors', v_anchors,
    'regulatory', v_reg,
    'memory', v_memory,
    'priors', v_priors,
    'warnings', to_jsonb(public.agent_text_array(to_jsonb(w))),
    'refs', v_refs,
    'versions', v_versions || jsonb_build_object(
      'elapsed_ms', round((extract(epoch from (clock_timestamp() - v_t0)) * 1000)::numeric, 1)));
end;
$$;
