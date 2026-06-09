-- Run in Supabase SQL Editor
-- Adds the enhanced_retrieval_enabled toggle to information_bot (controls whether
-- hybrid BM25+dense search with VoyageAI reranking is used during knowledge retrieval)
-- and inserts the knowledge_context_prompt into app_config so bot_chat.py can load it
-- at runtime without hardcoding any strings.

alter table if exists public.information_bot
  add column if not exists enhanced_retrieval_enabled boolean not null default false;

insert into public.app_config (key, value)
values (
  'knowledge_context_prompt',
  'KNOWLEDGE BASE CONTEXT
The following excerpts were retrieved from uploaded knowledge documents. Use them to support your answer where relevant. Cite only what is present in the excerpts — do not invent information beyond them.'
)
on conflict (key) do nothing;
