-- Run in Supabase SQL Editor
-- Sales Bot orders, line items, and payment audit/idempotency.
-- All money in integer minor units (paise for INR). Server always recomputes
-- totals from sales_products — client/order-message prices are never trusted.

create table if not exists public.sales_orders (
  id uuid primary key default gen_random_uuid(),
  order_number text not null unique,
  sender text not null,
  status text not null default 'draft'
    check (status in ('draft', 'pending_payment', 'paid', 'failed', 'cancelled', 'fulfilled')),
  subtotal_minor integer not null default 0,
  shipping_minor integer not null default 0,
  total_minor integer not null default 0,
  currency text not null default 'INR',
  customer_name text not null default '',
  customer_phone text not null default '',
  shipping_address jsonb not null default '{}'::jsonb,
  razorpay_order_id text,
  razorpay_payment_link_id text,
  razorpay_payment_link_url text,
  payment_status text not null default 'created',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.sales_orders disable row level security;

create index if not exists idx_sales_orders_sender on public.sales_orders (sender);
create index if not exists idx_sales_orders_status on public.sales_orders (status);

create table if not exists public.sales_order_items (
  id uuid primary key default gen_random_uuid(),
  order_id uuid not null references public.sales_orders(id) on delete cascade,
  product_id uuid,
  retailer_id text not null default '',
  name text not null default '',
  quantity integer not null default 1,
  unit_price_minor integer not null default 0,
  line_total_minor integer not null default 0
);

alter table public.sales_order_items disable row level security;

-- Payment audit + webhook idempotency. razorpay_event_id is unique so a
-- replayed/duplicate Razorpay webhook is a no-op (insert conflict).
create table if not exists public.sales_payments (
  id uuid primary key default gen_random_uuid(),
  order_id uuid references public.sales_orders(id) on delete set null,
  razorpay_event_id text not null unique,
  razorpay_payment_id text,
  razorpay_order_id text,
  razorpay_payment_link_id text,
  event_type text not null default '',
  amount_minor integer not null default 0,
  currency text not null default 'INR',
  status text not null default '',
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

alter table public.sales_payments disable row level security;

create or replace function public.set_sales_orders_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_sales_orders_updated_at on public.sales_orders;

create trigger trg_sales_orders_updated_at
before update on public.sales_orders
for each row
execute function public.set_sales_orders_updated_at();
