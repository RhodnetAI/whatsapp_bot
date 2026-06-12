-- Run in Supabase SQL Editor
-- Sales Bot catalog: the source-of-truth product table for the WhatsApp Sales
-- Agent. Kept SEPARATE from information_bot_products_services (which is
-- RAG/Qdrant-bound and stores free-text prices) so commerce concerns stay
-- isolated. Prices are stored as integer minor units (paise for INR) to avoid
-- floating-point money errors. `retailer_id` is the stable SKU and doubles as
-- the join key when syncing to the Meta Commerce Catalog.

create table if not exists public.sales_products (
  id uuid primary key default gen_random_uuid(),
  retailer_id text not null unique,
  name text not null default '',
  description text not null default '',
  category text not null default '',
  price_minor integer not null default 0,
  currency text not null default 'INR',
  image_url text not null default '',
  stock_quantity integer,                       -- null = unlimited stock
  is_active boolean not null default true,
  meta_catalog_id text,                         -- Meta product id once synced
  sync_status text not null default 'pending'
    check (sync_status in ('pending', 'synced', 'failed', 'disabled')),
  sync_error text not null default '',
  source text not null default 'manual'
    check (source in ('manual', 'imported')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.sales_products disable row level security;

create index if not exists idx_sales_products_category on public.sales_products (category);
create index if not exists idx_sales_products_active on public.sales_products (is_active);

create or replace function public.set_sales_products_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_sales_products_updated_at on public.sales_products;

create trigger trg_sales_products_updated_at
before update on public.sales_products
for each row
execute function public.set_sales_products_updated_at();
