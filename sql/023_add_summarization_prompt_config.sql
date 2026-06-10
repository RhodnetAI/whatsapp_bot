-- Run in Supabase SQL Editor
-- Adds the summarization_prompt entry used by the Instructions summarization flow

INSERT INTO public.app_config (key, value)
VALUES (
  'summarization_prompt',
  'Condense the provided instructions into a minimal system prompt.

Requirements:
- Preserve all rules, constraints, and behavioral requirements.
- Preserve all factual information and key details.
- Remove repetition, filler, explanations, and examples.
- Keep important structured information (services, features, pricing, policies).
- Do not omit facts required to answer user questions.
- Compress wording while keeping meaning intact.

Output format:
- Clear sections if present in the source.
- Bullet points for lists.
- Short sentences for descriptions.

Do not:
- Add new rules or interpretations.
- Change the meaning of the instructions.
- Introduce external knowledge.

The output must be directly usable as a system prompt for a chat model.'
)
ON CONFLICT (key) DO NOTHING;
