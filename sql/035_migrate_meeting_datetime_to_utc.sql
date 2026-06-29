-- One-time migration: convert existing meeting_bookings.meeting_datetime from
-- the old "fake-UTC" convention to true UTC.
--
-- Background: before the timezone fix, a 9:00 AM IST slot was stored as
-- 09:00:00+00:00 (i.e. the IST wall-clock time with a UTC label attached),
-- which is NOT the true UTC instant of that meeting. The application now
-- stores true UTC (a 9:00 AM IST meeting is 03:30:00+00:00) and converts to
-- IST for all display. Existing rows must be shifted back by the IST offset
-- (UTC+05:30) so they represent the same wall-clock moment under the new
-- convention.
--
-- RUN THIS EXACTLY ONCE, and only on a database that still holds pre-fix rows.
-- Running it a second time would shift the data by another 5h30m. There is no
-- automatic guard; apply it in the Supabase SQL editor as a deliberate step
-- (consistent with how the other migrations in this folder are applied).

update public.meeting_bookings
set meeting_datetime = meeting_datetime - interval '5 hours 30 minutes';
