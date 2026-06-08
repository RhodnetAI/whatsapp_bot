-- Adds enable/disable toggle support for the Products & Services and Scheduler
-- sections, consistent with the existing company_info_enabled /
-- flow_creation_enabled / knowledge_enabled columns on information_bot.
--
-- These sections each manage their own multi-row tables
-- (information_bot_products_services / information_bot_scheduler), so the
-- single on/off toggle for the section lives on the singleton information_bot
-- row alongside the other section toggles rather than on those tables.

alter table if exists public.information_bot
  add column if not exists products_services_enabled boolean not null default false;

alter table if exists public.information_bot
  add column if not exists scheduler_enabled boolean not null default false;
