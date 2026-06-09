-- Migration 020: add calendar event tracking columns to meeting_bookings
ALTER TABLE public.meeting_bookings
  ADD COLUMN IF NOT EXISTS calendar_event_id   TEXT,
  ADD COLUMN IF NOT EXISTS calendar_event_link TEXT;
