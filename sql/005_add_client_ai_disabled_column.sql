-- Migration: Add AI toggle flag to whatsapp_conversations if missing
-- Run this in Supabase SQL Editor if existing rows do not yet have ai_disabled.

ALTER TABLE IF EXISTS public.whatsapp_conversations
ADD COLUMN IF NOT EXISTS ai_disabled boolean NOT NULL DEFAULT false;