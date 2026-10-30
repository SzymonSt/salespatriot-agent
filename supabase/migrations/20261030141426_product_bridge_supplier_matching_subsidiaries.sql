-- A prefix name match ("Rheinmetall Waffe Munition" vs "... Schweiz AG") may be a subsidiary in another
-- country, i.e. a different legal entity: allow it only within the same country. Exact name keys may still
-- link across countries when the name is distinctive (>= 12 characters).
create or replace function public.product_bridge_match_supplier(p_identifier text, p_name text, p_country text)
returns text
language sql
stable
set search_path = ''
as $$
  with c as (
    select s.id,
           (public.product_bridge_digits(p_identifier) is not null
            and public.product_bridge_digits(s.identifier) = public.product_bridge_digits(p_identifier)) as id_match,
           public.product_bridge_name_key(p_name) in (public.product_bridge_name_key(s.legal_name),
                                                      public.product_bridge_name_key(s.name)) as name_exact,
           (public.product_bridge_names_compatible(p_name, s.legal_name)
            or public.product_bridge_names_compatible(p_name, s.name)) as name_match,
           coalesce(s.country = p_country, true) as same_country,
           coalesce(length(public.product_bridge_name_key(p_name)), 0) as key_len
    from public.suppliers s
  )
  select id from c
  where name_match
    and (same_country or (name_exact and (id_match or key_len >= 12)))
  order by id_match desc, name_exact desc, same_country desc, id
  limit 1
$$;

revoke all on function public.product_bridge_match_supplier(text, text, text) from public, anon, authenticated;
