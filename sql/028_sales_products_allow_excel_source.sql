-- Run in Supabase SQL Editor
-- Sales Bot products are now created by manual add or Excel upload (the
-- "import from Information Bot" path was removed). Allow source = 'excel'
-- and drop the now-unused 'imported' value.

alter table public.sales_products drop constraint if exists sales_products_source_check;

alter table public.sales_products
  add constraint sales_products_source_check check (source in ('manual', 'excel'));
