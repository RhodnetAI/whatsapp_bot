-- Run in Supabase SQL Editor
-- Sales Bot cart (FALLBACK path only). When the Meta Commerce Catalog is
-- connected, the cart lives natively inside WhatsApp and arrives as an `order`
-- webhook — these tables are NOT used in that path. They back the DB-driven
-- cart used when no catalog is connected yet (browse via List/Buttons).

create table if not exists public.sales_carts (
  id uuid primary key default gen_random_uuid(),
  sender text not null unique,
  status text not null default 'active'
    check (status in ('active', 'checked_out', 'abandoned')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.sales_carts disable row level security;

create table if not exists public.sales_cart_items (
  id uuid primary key default gen_random_uuid(),
  cart_id uuid not null references public.sales_carts(id) on delete cascade,
  product_id uuid not null references public.sales_products(id) on delete cascade,
  quantity integer not null default 1,
  unit_price_minor integer not null default 0,   -- snapshot at add-to-cart time
  created_at timestamptz not null default now(),
  unique (cart_id, product_id)
);

alter table public.sales_cart_items disable row level security;

create or replace function public.set_sales_carts_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_sales_carts_updated_at on public.sales_carts;

create trigger trg_sales_carts_updated_at
before update on public.sales_carts
for each row
execute function public.set_sales_carts_updated_at();
