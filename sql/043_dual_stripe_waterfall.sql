-- 043_dual_stripe_waterfall.sql
-- Add Dual Stripe account waterfall routing columns to campaigns

ALTER TABLE public.campaigns
  ADD COLUMN IF NOT EXISTS stripe_routing_mode text DEFAULT 'single',
  ADD COLUMN IF NOT EXISTS stripe_new_account_id text,
  ADD COLUMN IF NOT EXISTS stripe_old_account_id text,
  ADD COLUMN IF NOT EXISTS stripe_new_daily_limit numeric;

COMMENT ON COLUMN public.campaigns.stripe_routing_mode IS 'Stripe routing mode: single or dual_limit (waterfall)';
COMMENT ON COLUMN public.campaigns.stripe_new_account_id IS 'New Stripe account ID for daily limit allocation';
COMMENT ON COLUMN public.campaigns.stripe_old_account_id IS 'Old/Fallback Stripe account ID when daily limit is exceeded';
COMMENT ON COLUMN public.campaigns.stripe_new_daily_limit IS 'Max daily donation volume (GBP £) before switching to old account';
