-- Adds WhatsApp/Email admin notification toggles for the Flow Creation section.
-- When a user completes the flow, these control whether the admin (configured
-- via ADMIN_WHATSAPP_NUMBER / ADMIN_NOTIFICATION_EMAIL env vars) is notified —
-- same admin contact env vars as the Scheduler section's notification toggles.

alter table if exists public.information_bot
  add column if not exists flow_notify_whatsapp_enabled boolean not null default false;

alter table if exists public.information_bot
  add column if not exists flow_notify_email_enabled boolean not null default false;
