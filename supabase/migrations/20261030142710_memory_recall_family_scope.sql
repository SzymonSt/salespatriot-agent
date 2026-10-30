-- WS4 fix: memory_recall family scope. A family query matches entries with no family, the same family, or the
-- family's class (class-level memory); sibling families of the same class no longer match (never mix 9x19
-- ammunition with 155 mm artillery). A class query (tender not classified to a family yet) matches every
-- family of that class.
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
  v_limit    int := greatest(1, least(coalesce(p_limit, 5), 50));
  v_class    text;
  v_is_class boolean := false;
  v_result   jsonb;
begin
  if p_family_id is not null then
    select coalesce(f.parent_id, f.id), f.parent_id is null
      into v_class, v_is_class
    from public.product_families f where f.id = p_family_id;
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
           or m.family_id = v_class
           or (coalesce(v_is_class, false) and mf.parent_id = v_class))
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

revoke all on function public.memory_recall(extensions.vector, text, text, text, text, text, int)
  from public, anon, authenticated;
grant execute on function public.memory_recall(extensions.vector, text, text, text, text, text, int) to service_role;
