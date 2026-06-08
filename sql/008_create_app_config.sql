-- Run in Supabase SQL Editor
-- Creates app_config table for storing application-level configuration values

CREATE TABLE IF NOT EXISTS public.app_config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.app_config DISABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION public.set_app_config_updated_at()
RETURNS TRIGGER
LANGUAGE PLPGSQL
AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_app_config_updated_at ON public.app_config;

CREATE TRIGGER trg_app_config_updated_at
BEFORE UPDATE ON public.app_config
FOR EACH ROW
EXECUTE FUNCTION public.set_app_config_updated_at();

-- Summarizer system prompt
INSERT INTO public.app_config (key, value)
VALUES (
  'summarizer_system_prompt',
  'Condense the provided instructions into a minimal system prompt.

Requirements:
• Preserve all rules, constraints, and behavioral requirements.
• Preserve all factual information and key details.
• Remove repetition, filler, explanations, and examples.
• Keep important structured information (services, features, pricing, policies).
• Do not omit facts required to answer user questions.
• Compress wording while keeping meaning intact.

Output format:
• Clear sections if present in the source.
• Bullet points for lists.
• Short sentences for descriptions.

Do not:
• Add new rules or interpretations.
• Change the meaning of the instructions.
• Introduce external knowledge.

The output must be directly usable as a system prompt for a chat model.'
)
ON CONFLICT (key) DO NOTHING;
