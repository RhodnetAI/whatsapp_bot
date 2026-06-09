-- Meeting bookings table.
-- Stores confirmed meeting bookings created through the scheduler flow.
-- Time slots are blocked by checking this table for overlaps before showing
-- available slots to new users.

create table if not exists public.meeting_bookings (
  id uuid primary key default gen_random_uuid(),
  sender text not null,
  user_name text not null,
  user_email text not null,
  meeting_datetime timestamptz not null,
  duration_minutes integer not null default 30,
  meet_link text,
  conversation_summary text,
  created_at timestamptz not null default now()
);

alter table public.meeting_bookings disable row level security;

create index if not exists idx_meeting_bookings_sender on public.meeting_bookings (sender);
create index if not exists idx_meeting_bookings_meeting_datetime on public.meeting_bookings (meeting_datetime);
