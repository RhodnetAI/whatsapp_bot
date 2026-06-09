-- Meeting booking tables for the Information Bot scheduler flow.
--
-- meeting_bookings: confirmed bookings with Google Meet link and conversation summary.
-- meeting_session_state: per-sender per-day flow state machine (resets each UTC day).

-- ── meeting_bookings ─────────────────────────────────────────────────────────

create table if not exists public.meeting_bookings (
  id uuid primary key default gen_random_uuid(),
  sender text not null,
  user_name text not null default '',
  user_email text not null default '',
  meeting_date text not null default '',
  meeting_time text not null default '',
  duration_minutes integer not null default 60,
  conversation_summary text not null default '',
  meeting_link text not null default '',
  created_at timestamptz not null default now()
);

alter table public.meeting_bookings disable row level security;

-- ── meeting_session_state ────────────────────────────────────────────────────
--
-- One row per (sender, state_date). state_date is today's UTC date as YYYY-MM-DD.
-- A new day naturally starts a fresh idle row — declined_today resets automatically.
--
-- flow_step values:
--   idle | asked_yes_no | showing_slots | asked_duration |
--   asked_confirm | asked_name_email | completed | declined

create table if not exists public.meeting_session_state (
  id uuid primary key default gen_random_uuid(),
  sender text not null,
  state_date text not null,
  flow_step text not null default 'idle',
  partial_data jsonb not null default '{}'::jsonb,
  declined_today boolean not null default false,
  suggestion_made boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint meeting_session_state_sender_date_key unique (sender, state_date)
);

alter table public.meeting_session_state disable row level security;
