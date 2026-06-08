-- Run in Supabase SQL Editor
-- Conversational memory, session/greeting detection, and admin-message
-- visibility for the bot AI now derive entirely from
-- whatsapp_conversations.conversation (see app/services/bot_chat.py), so the
-- separate per-bot history tables created in
-- sql/013_create_bot_conversation_history_tables.sql are no longer used.
-- whatsapp_conversations is the single source of truth for conversation data.

drop table if exists public.information_bot_conversations;
drop table if exists public.sales_bot_conversations;
