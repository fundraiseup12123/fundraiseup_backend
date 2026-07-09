-- Per-view payment accounts for root homepage and pop-up view
ALTER TABLE public.campaign_content
  ADD COLUMN IF NOT EXISTS payment_accounts_json text;