-- Run this in Supabase SQL Editor to create the onboarding/setup tables.

-- If the table already exists with the old schema, run this migration first:
-- ALTER TABLE public.service_agent_documents DROP COLUMN IF EXISTS content_text;

create extension if not exists pgcrypto;

create table if not exists public.service_agent_setup (
  id integer primary key,
  setup_completed boolean not null default false,
  main_instruction text not null default '',
  dos text not null default '',
  donts text not null default '',
  flow_builder jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Add the flow_builder column to older schemas if it is missing.
alter table if exists public.service_agent_setup add column if not exists flow_builder jsonb not null default '{}'::jsonb;

-- Disable RLS for setup table (managed server-side)
alter table public.service_agent_setup disable row level security;

insert into public.service_agent_setup (id)
values (1)
on conflict (id) do nothing;

create or replace function public.set_service_agent_setup_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_service_agent_setup_updated_at on public.service_agent_setup;

create trigger trg_service_agent_setup_updated_at
before update on public.service_agent_setup
for each row
execute function public.set_service_agent_setup_updated_at();

create table if not exists public.service_agent_documents (
  id uuid primary key default gen_random_uuid(),
  setup_id integer not null references public.service_agent_setup(id) on delete cascade default 1,
  file_name text not null,
  original_name text not null,
  mime_type text,
  bucket_path text not null,
  char_count integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Disable RLS for documents table (managed server-side)
alter table public.service_agent_documents disable row level security;

create index if not exists idx_service_agent_documents_setup_id
  on public.service_agent_documents (setup_id);

create index if not exists idx_service_agent_documents_created_at
  on public.service_agent_documents (created_at desc);

create or replace function public.set_service_agent_documents_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_service_agent_documents_updated_at on public.service_agent_documents;

create trigger trg_service_agent_documents_updated_at
before update on public.service_agent_documents
for each row
execute function public.set_service_agent_documents_updated_at();

-- Storage bucket setup
-- Run these commands in Supabase SQL Editor:
-- 1. Create bucket (if not exists):
INSERT INTO storage.buckets (id, name, public)
VALUES ('service-agent-documents', 'service-agent-documents', false)
ON CONFLICT (id) DO NOTHING;

-- 2. Create storage policies to allow service role uploads:
-- Drop existing policies if needed:
DROP POLICY IF EXISTS "service-agent-documents all" ON storage.objects;

-- Create new policy allowing all operations:
CREATE POLICY "service-agent-documents all"
ON storage.objects
FOR ALL
USING (bucket_id = 'service-agent-documents')
WITH CHECK (bucket_id = 'service-agent-documents');
