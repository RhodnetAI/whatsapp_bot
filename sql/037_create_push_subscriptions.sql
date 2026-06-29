-- Push notification device tokens for the React Native receiver app.
-- Each row is one Expo push token (one per device/install). The webhook sends
-- a push to every registered token whenever a new inbound WhatsApp message
-- lands, so the admin is notified even when the app is closed.

create table if not exists push_subscriptions (
    id uuid primary key default gen_random_uuid(),
    token text not null unique,
    platform text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists push_subscriptions_token_idx
    on push_subscriptions (token);
