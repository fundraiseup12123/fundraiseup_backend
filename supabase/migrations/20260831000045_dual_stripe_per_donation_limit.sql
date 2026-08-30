-- 045_dual_stripe_per_donation_limit.sql
-- Add per-donation limit for Dual Stripe waterfall routing

ALTER TABLE public.campaigns
  ADD COLUMN IF NOT EXISTS stripe_new_per_donation_limit numeric;

COMMENT ON COLUMN public.campaigns.stripe_new_per_donation_limit IS 'Max per-donation volume (GBP £) for new account before routing to old account';
