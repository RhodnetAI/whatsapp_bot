-- Make whatsapp_conversations.conversation_date track the IST calendar day.
--
-- Background: conversation_date was set from Postgres `current_date`, which on
-- Supabase is the UTC date. The rest of the app now reasons in IST days
-- (is_first_message_of_session, the Sidebar "today" check, the chat history
-- date grouping), so a UTC conversation_date is off by up to 5h30m and can land
-- on the wrong day for messages between IST midnight and 05:30. Compute it in
-- Asia/Kolkata instead so the stored day matches what users see.
--
-- 'Asia/Kolkata' is a fixed UTC+05:30 (no DST) and is always present in
-- pg_timezone_names, so this is exact. Safe to run more than once.

alter table public.whatsapp_conversations
  alter column conversation_date set default ((now() at time zone 'Asia/Kolkata')::date);

create or replace function public.set_updated_at_and_conversation_date()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  new.conversation_date = (now() at time zone 'Asia/Kolkata')::date;
  return new;
end;
$$;
