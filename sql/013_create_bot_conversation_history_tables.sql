-- Run in Supabase SQL Editor
-- Per-bot conversation history used to build AI context (separate stores so
-- Information Agent and Sales Agent never share data).
--
-- Each row is a single message. A "session" is one calendar day per phone
-- number: session_date groups a phone number's messages by the day they were
-- sent, so the first message seen for a phone number on a given session_date
-- marks the start of a new session (and triggers the greeting).

create extension if not exists pgcrypto;

-- ── information_bot_conversations ────────────────────────────────────────────

create table if not exists public.information_bot_conversations (
  id uuid primary key default gen_random_uuid(),
  phone_number text not null,
  role text not null check (role in ('user', 'assistant')),
  message text not null,
  session_date date not null default current_date,
  created_at timestamptz not null default now()
);

create index if not exists idx_information_bot_conversations_phone_session
  on public.information_bot_conversations (phone_number, session_date, created_at);

alter table public.information_bot_conversations disable row level security;

-- ── sales_bot_conversations ──────────────────────────────────────────────────

create table if not exists public.sales_bot_conversations (
  id uuid primary key default gen_random_uuid(),
  phone_number text not null,
  role text not null check (role in ('user', 'assistant')),
  message text not null,
  session_date date not null default current_date,
  created_at timestamptz not null default now()
);

create index if not exists idx_sales_bot_conversations_phone_session
  on public.sales_bot_conversations (phone_number, session_date, created_at);

alter table public.sales_bot_conversations disable row level security;
