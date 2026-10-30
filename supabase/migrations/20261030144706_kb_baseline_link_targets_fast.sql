-- kb_baseline_link_targets as one query per mode: the union of both modes defeated the ordered index scan and
-- took ~4 s per call (PostgREST statement_timeout is 8 s). Partial indexes make both probes index-only.
create index if not exists kb_baseline_notices_comp_procedure
  on public.kb_baseline_notices (snapshot_id, procedure_id) where kind = 'competition';
create index if not exists kb_baseline_notices_res_procedure
  on public.kb_baseline_notices (snapshot_id, procedure_id) where kind in ('result', 'modification') and procedure_id is not null;

create or replace function public.kb_baseline_link_targets(p_snapshot text, p_mode text, p_after text default null,
                                                           p_limit int default 40)
returns text[]
language plpgsql
stable
security definer
set search_path = ''
as $$
declare
  v_ids text[];
begin
  if p_mode = 'procedure' then
    select coalesce(array_agg(s.x order by s.x), '{}') into v_ids
    from (
      select distinct r.procedure_id as x
      from public.kb_baseline_notices r
      where r.snapshot_id = p_snapshot and r.kind in ('result', 'modification')
        and r.procedure_id is not null and r.procedure_id > coalesce(p_after, '')
        and not exists (select 1 from public.kb_baseline_notices c
                        where c.snapshot_id = p_snapshot and c.kind = 'competition' and c.procedure_id = r.procedure_id)
      order by 1
      limit greatest(p_limit, 1)
    ) s;
  elsif p_mode = 'previous' then
    select coalesce(array_agg(s.x order by s.x), '{}') into v_ids
    from (
      select distinct ltrim(p.x, '0') as x
      from public.kb_baseline_notices r, unnest(r.previous_notice_ids) as p(x)
      where r.snapshot_id = p_snapshot and r.kind in ('result', 'modification')
        and cardinality(r.previous_notice_ids) > 0
        and p.x ~ '^[0-9]{1,8}-[0-9]{4}$' and ltrim(p.x, '0') > coalesce(p_after, '')
        and not exists (select 1 from public.kb_baseline_notices c
                        where c.snapshot_id = p_snapshot and c.publication_number = ltrim(p.x, '0'))
      order by 1
      limit greatest(p_limit, 1)
    ) s;
  else
    raise exception 'kb_baseline_link_targets: unknown mode %', p_mode;
  end if;
  return v_ids;
end
$$;
revoke all on function public.kb_baseline_link_targets(text, text, text, int) from public, anon, authenticated;
grant execute on function public.kb_baseline_link_targets(text, text, text, int) to service_role;
