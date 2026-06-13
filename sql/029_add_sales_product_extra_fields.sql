-- Run in Supabase SQL Editor
-- Sales Bot products: discount/sale price + extra catalog attributes
-- (Google product category, size, model, color, delivery date, quantity to
-- sell, rating average/count). google_product_category is required going
-- forward for manual add/edit and Excel upload (enforced in the API layer);
-- existing rows default to '' until edited.

alter table public.sales_products
  add column if not exists discount_percentage numeric(5,2) not null default 0
    check (discount_percentage >= 0 and discount_percentage <= 100),
  add column if not exists sale_price_minor integer not null default 0,
  add column if not exists google_product_category text not null default '',
  add column if not exists size text not null default '',
  add column if not exists model text not null default '',
  add column if not exists color text not null default '',
  add column if not exists delivery_date date,
  add column if not exists quantity_to_sell integer,
  add column if not exists rating_average numeric(3,2)
    check (rating_average is null or (rating_average >= 0 and rating_average <= 5)),
  add column if not exists rating_count integer;

-- Backfill: with discount_percentage = 0, sale price equals the regular price.
update public.sales_products set sale_price_minor = price_minor where sale_price_minor = 0;
