-- Supplier matching for TED award winners (knowledge_base/27-ontology-integration-guide.md).
-- An identifier counts only when the names agree too: TED sometimes gives a winner another company's
-- identifier (e.g. Vortex Eesti with Baltic Fox's, ZM Tarnów with Łucznik's). A distinctive name alone is
-- enough. Names are compared without legal forms, accents and punctuation ("Fiocchi Munizioni S.p.A." =
-- "FIOCCHI MUNIZIONI SPA").

create or replace function public.product_bridge_name_key(p_value text)
returns text
language sql
immutable
set search_path = ''
as $$
  select nullif(replace(regexp_replace(
           regexp_replace(
             translate(lower(btrim(coalesce(p_value, ''))),
               'áàâäãåāăąçćčďđéèêëēėęěíìîïīįłľĺńňñóòôöõőøřśšşțťúùûüūůűýÿźżž',
               'aaaaaaaaacccddeeeeeeeeiiiiiilllnnnooooooorsssttuuuuuuuyyzzz'),
             '[^a-z0-9]+', ' ', 'g'),
           '(\s(sa|as|a s|s a|spa|s p a|srl|s r l|sro|s r o|gmbh|gbmh|ag|ltd|limited|bv|b v|nv|sl|slu|sas|sarl|oy|ou|ab|kg|co|inc|llc|plc|doo|d o o|lda|sp z o o|spzoo|sp akcyjna|spolka akcyjna|pty|se))+\s*$',
           ''), ' ', ''), '')
$$;

create or replace function public.product_bridge_names_compatible(p_a text, p_b text)
returns boolean
language sql
immutable
set search_path = ''
as $$
  with k as (select public.product_bridge_name_key(p_a) as a, public.product_bridge_name_key(p_b) as b)
  select coalesce(a = b or (least(length(a), length(b)) >= 8 and (a like b || '%' or b like a || '%')), false)
  from k
$$;

-- Link only when the names agree; then prefer an identifier match, then the same country.
-- A name shorter than 12 characters also needs the same country (or no country on one side).
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
           (public.product_bridge_names_compatible(p_name, s.legal_name)
            or public.product_bridge_names_compatible(p_name, s.name)) as name_match,
           coalesce(s.country = p_country, true) as same_country,
           coalesce(length(public.product_bridge_name_key(p_name)), 0) as key_len
    from public.suppliers s
  )
  select id from c
  where name_match and (id_match or same_country or key_len >= 12)
  order by id_match desc, same_country desc, id
  limit 1
$$;

revoke all on function public.product_bridge_name_key(text) from public, anon, authenticated;
revoke all on function public.product_bridge_names_compatible(text, text) from public, anon, authenticated;
revoke all on function public.product_bridge_match_supplier(text, text, text) from public, anon, authenticated;
