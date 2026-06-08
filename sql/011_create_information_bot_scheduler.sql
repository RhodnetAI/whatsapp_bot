-- Scheduler for the Information Bot.
--
-- Stores regular working hours, exclusion times (e.g., lunch breaks),
-- and special hours (holidays, specific overrides). Each entry is embedded
-- into Qdrant for RAG retrieval by the Information Agent, allowing it to
-- answer questions about business hours (see app/services/vectorizer.py —
-- vectors are linked back here via payload.document_id = id).

create table if not exists public.information_bot_scheduler (
  id uuid primary key default gen_random_uuid(),
  day_of_week text not null check (day_of_week in ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')),
  time_start text not null default '09:00',
  time_end text not null default '17:00',
  exclude_time_start text not null default '',
  exclude_time_end text not null default '',
  is_special_time boolean not null default false,
  special_date text not null default '',
  source text not null default 'manual' check (source in ('manual')),
  vectorization_status text not null default 'processing' check (vectorization_status in ('processing', 'done', 'failed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.information_bot_scheduler disable row level security;
