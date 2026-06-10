-- Replace conversation_summary with a user-supplied meeting purpose.

ALTER TABLE public.meeting_bookings
  ADD COLUMN IF NOT EXISTS purpose TEXT;

ALTER TABLE public.meeting_bookings
  DROP COLUMN IF EXISTS conversation_summary;
