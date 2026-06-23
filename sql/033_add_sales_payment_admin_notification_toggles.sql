-- Adds WhatsApp/Email admin notification toggles for the Sales Bot's payment
-- completion. When a customer's payment is confirmed (Razorpay webhook), these
-- control whether the admin (configured via ADMIN_WHATSAPP_NUMBER /
-- ADMIN_NOTIFICATION_EMAIL env vars) is notified — same admin contact env vars
-- as the Scheduler / Flow Creation sections' notification toggles.

alter table if exists public.sales_bot
  add column if not exists sales_payment_notify_whatsapp_enabled boolean not null default false;

alter table if exists public.sales_bot
  add column if not exists sales_payment_notify_email_enabled boolean not null default false;
