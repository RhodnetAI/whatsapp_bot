-- Run in Supabase SQL Editor
-- Commerce configuration columns for the Sales Bot singleton row.
-- (conversation_history_enabled may already exist from migration 018 — the
-- IF NOT EXISTS guard makes this safe to run regardless.)

alter table public.sales_bot
  add column if not exists sales_products_enabled boolean not null default false,
  add column if not exists payment_enabled boolean not null default false,
  add column if not exists default_currency text not null default 'INR',
  add column if not exists flat_shipping_minor integer not null default 0,
  add column if not exists free_shipping_threshold_minor integer,
  add column if not exists conversation_history_enabled boolean not null default true;
