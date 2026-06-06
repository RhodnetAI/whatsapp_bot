-- Run in Supabase SQL Editor
ALTER TABLE service_agent_setup
  ADD COLUMN IF NOT EXISTS bot_name TEXT,
  ADD COLUMN IF NOT EXISTS greeting TEXT,
  ADD COLUMN IF NOT EXISTS summarized_instruction TEXT,
  ADD COLUMN IF NOT EXISTS instruction_hash TEXT,
  ADD COLUMN IF NOT EXISTS company_address TEXT,
  ADD COLUMN IF NOT EXISTS company_phone TEXT,
  ADD COLUMN IF NOT EXISTS company_email TEXT,
  ADD COLUMN IF NOT EXISTS social_handles JSONB DEFAULT '[]'::JSONB;
