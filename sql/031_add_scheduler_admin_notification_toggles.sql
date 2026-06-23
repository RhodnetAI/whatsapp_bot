-- Adds WhatsApp/Email admin notification toggles for the Scheduler section.
-- When a meeting is booked, these control whether the admin (configured via
-- ADMIN_WHATSAPP_NUMBER / ADMIN_NOTIFICATION_EMAIL env vars) is notified.

alter table if exists public.information_bot
  add column if not exists scheduler_notify_whatsapp_enabled boolean not null default false;

alter table if exists public.information_bot
  add column if not exists scheduler_notify_email_enabled boolean not null default false;
