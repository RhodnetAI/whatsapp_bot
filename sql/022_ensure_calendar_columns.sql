-- Ensure calendar_event_id / calendar_event_link columns exist on
-- meeting_bookings (idempotent re-run of 020) and refresh PostgREST's
-- schema cache so it picks them up immediately.

ALTER TABLE public.meeting_bookings
  ADD COLUMN IF NOT EXISTS calendar_event_id TEXT;

ALTER TABLE public.meeting_bookings
  ADD COLUMN IF NOT EXISTS calendar_event_link TEXT;

NOTIFY pgrst, 'reload schema';
