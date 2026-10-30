-- Link product_items to the company-profile tables (suppliers, buyers) and to the exact source row,
-- so the ontology can be fed from those tables by triggers (knowledge_base/27-ontology-integration-guide.md).

alter table public.product_items
  add column if not exists supplier_id text references public.suppliers (id) on delete set null,
  add column if not exists buyer_id text references public.buyers (id) on delete set null,
  add column if not exists source_table text,      -- 'supplier_contracts' | 'suppliers.catalogue' | 'ted_notices' | 'ted_awards' | 'platform'
  add column if not exists source_pk text,         -- key of the source row in that table
  add column if not exists source_index int,       -- position in suppliers.catalogue->'items' (for the UI)
  add column if not exists source_seen_at timestamptz;  -- last time the bridge saw the source row (orphan cleanup)

create index if not exists product_items_supplier on public.product_items (supplier_id);
create index if not exists product_items_buyer on public.product_items (buyer_id);
create index if not exists product_items_publication on public.product_items (publication_number);
create index if not exists product_items_source on public.product_items (source_table, source_pk);
