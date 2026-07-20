-- Per-campaign checkout rail override. NULL = inherit organization payment_account_sources.
ALTER TABLE public.campaigns
  ADD COLUMN IF NOT EXISTS payment_account_sources jsonb;
