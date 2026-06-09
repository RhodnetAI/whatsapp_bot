ALTER TABLE information_bot
ADD COLUMN IF NOT EXISTS conversation_history_enabled boolean NOT NULL DEFAULT true;

ALTER TABLE sales_bot
ADD COLUMN IF NOT EXISTS conversation_history_enabled boolean NOT NULL DEFAULT true;
