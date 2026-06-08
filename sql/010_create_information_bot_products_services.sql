-- Products and Services for the Information Bot.
--
-- Each row is either a "product" or a "service" (discriminated by `kind`),
-- holds all fields surfaced in the Settings UI, and is the source of truth
-- that gets embedded into Qdrant for RAG retrieval by the Information Agent
-- (see app/services/vectorizer.py — vectors are linked back here via
-- payload.document_id = id, the same pattern used for knowledge documents).

create table if not exists public.information_bot_products_services (
  id uuid primary key default gen_random_uuid(),
  kind text not null check (kind in ('product', 'service')),
  name text not null default '',
  short_description text not null default '',
  full_description text not null default '',
  category text not null default '',
  price text not null default '',
  discount_price text not null default '',
  status text not null default 'Active' check (status in ('Active', 'Out of Stock')),
  images text not null default '',
  rating text not null default '',
  reviews_count text not null default '',
  purchased_count text not null default '',
  source text not null default 'manual' check (source in ('manual', 'excel')),
  vectorization_status text not null default 'processing' check (vectorization_status in ('processing', 'done', 'failed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.information_bot_products_services disable row level security;
