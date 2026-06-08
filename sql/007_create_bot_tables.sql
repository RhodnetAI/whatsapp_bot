-- Run in Supabase SQL Editor
-- Creates information_bot and sales_bot tables to replace service_agent_setup

-- ── information_bot ──────────────────────────────────────────────────────────

create table if not exists public.information_bot (
  id integer primary key default 1,
  is_selected boolean not null default true,
  bot_name text,
  greeting text,
  main_instruction text not null default '',
  dos text not null default '',
  donts text not null default '',
  summarized_instruction text,
  instruction_hash text,
  company_address text,
  company_phone text,
  company_email text,
  social_handles jsonb not null default '[]'::jsonb,
  flow_builder jsonb not null default '{}'::jsonb,
  company_info_enabled boolean not null default false,
  flow_creation_enabled boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.information_bot disable row level security;

insert into public.information_bot (id, is_selected)
values (1, true)
on conflict (id) do nothing;

create or replace function public.set_information_bot_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_information_bot_updated_at on public.information_bot;

create trigger trg_information_bot_updated_at
before update on public.information_bot
for each row
execute function public.set_information_bot_updated_at();

-- ── sales_bot ────────────────────────────────────────────────────────────────

create table if not exists public.sales_bot (
  id integer primary key default 1,
  is_selected boolean not null default false,
  bot_name text,
  greeting text,
  main_instruction text not null default '',
  dos text not null default '',
  donts text not null default '',
  summarized_instruction text,
  instruction_hash text,
  company_address text,
  company_phone text,
  company_email text,
  social_handles jsonb not null default '[]'::jsonb,
  company_info_enabled boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.sales_bot disable row level security;

insert into public.sales_bot (id, is_selected)
values (1, false)
on conflict (id) do nothing;

create or replace function public.set_sales_bot_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_sales_bot_updated_at on public.sales_bot;

create trigger trg_sales_bot_updated_at
before update on public.sales_bot
for each row
execute function public.set_sales_bot_updated_at();
