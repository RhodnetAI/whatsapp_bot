-- Migration: Add flow_builder column to service_agent_setup if missing
-- Run this in Supabase SQL Editor if you get: 
-- "Could not find the 'flow_builder' column of 'service_agent_setup' in the schema cache"

-- Add column if it doesn't exist
ALTER TABLE IF EXISTS public.service_agent_setup 
ADD COLUMN IF NOT EXISTS flow_builder jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Refresh the schema cache (PostgREST should pick up the new column automatically)
-- If issues persist, you may need to:
-- 1. Go to Supabase dashboard
-- 2. Settings > API Settings
-- 3. Click "Refresh" or wait 30 seconds for auto-refresh
