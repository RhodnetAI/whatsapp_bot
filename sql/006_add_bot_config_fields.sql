-- Add bot_name and greeting_message to service_agent_setup.
-- Run this in Supabase SQL Editor.

alter table public.service_agent_setup
  add column if not exists bot_name text not null default '',
  add column if not exists greeting_message text not null default '';