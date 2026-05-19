-- Run this in Supabase SQL Editor for your project.
-- Creates a separate table to persist confirmed flow details linked to a conversation.

create table if not exists public.whatsapp_flow_confirmations (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references public.whatsapp_conversations(id) on delete cascade,
  sender text not null,
  details jsonb not null default '{}'::jsonb,
  confirmed_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create unique index if not exists idx_whatsapp_flow_confirmations_conversation_id
  on public.whatsapp_flow_confirmations (conversation_id);

create index if not exists idx_whatsapp_flow_confirmations_sender
  on public.whatsapp_flow_confirmations (sender);

create or replace function public.set_whatsapp_flow_confirmations_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_whatsapp_flow_confirmations_updated_at on public.whatsapp_flow_confirmations;

create trigger trg_whatsapp_flow_confirmations_updated_at
before update on public.whatsapp_flow_confirmations
for each row
execute function public.set_whatsapp_flow_confirmations_updated_at();
