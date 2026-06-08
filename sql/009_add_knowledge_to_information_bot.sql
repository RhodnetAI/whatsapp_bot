-- Adds Knowledge section support directly on information_bot:
-- a toggle column (consistent with company_info_enabled / flow_creation_enabled)
-- and a JSONB array column holding uploaded document metadata
-- (same pattern as the existing flow_builder / social_handles columns).
--
-- This replaces the previous mapping that stored documents in a separate
-- service_agent_documents table.

alter table if exists public.information_bot
  add column if not exists knowledge_enabled boolean not null default false;

alter table if exists public.information_bot
  add column if not exists knowledge_documents jsonb not null default '[]'::jsonb;

-- Storage bucket for the uploaded file bytes (binary content lives in Storage,
-- document metadata lives in the knowledge_documents column above).
insert into storage.buckets (id, name, public)
values ('service-agent-documents', 'service-agent-documents', false)
on conflict (id) do nothing;

drop policy if exists "service-agent-documents all" on storage.objects;

create policy "service-agent-documents all"
on storage.objects
for all
using (bucket_id = 'service-agent-documents')
with check (bucket_id = 'service-agent-documents');
