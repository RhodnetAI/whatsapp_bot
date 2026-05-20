-- Run this in Supabase SQL Editor for your project.
-- It creates the table required by the backend routes.

create extension if not exists pgcrypto;

create table if not exists public.whatsapp_conversations (
  id uuid primary key default gen_random_uuid(),
  sender text not null unique,
  client_name text,
  conversation jsonb not null default '[]'::jsonb,
  unread boolean not null default false,
  bookmarked boolean not null default false,
  blocked boolean not null default false,
  lead_label text not null default 'general',
  conversation_date date not null default current_date,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_whatsapp_conversations_sender
  on public.whatsapp_conversations (sender);

create index if not exists idx_whatsapp_conversations_updated_at
  on public.whatsapp_conversations (updated_at desc);

create index if not exists idx_whatsapp_conversations_conversation_date
  on public.whatsapp_conversations (conversation_date);

create or replace function public.set_updated_at_and_conversation_date()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  new.conversation_date = current_date;
  return new;
end;
$$;

drop trigger if exists trg_whatsapp_conversations_updated_at on public.whatsapp_conversations;

create trigger trg_whatsapp_conversations_updated_at
before update on public.whatsapp_conversations
for each row
execute function public.set_updated_at_and_conversation_date();
