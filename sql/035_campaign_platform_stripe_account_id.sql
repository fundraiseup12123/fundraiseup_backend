-- Optional platform Stripe pool entry used when campaign payment_account_sources.stripe = platform.
-- NULL means use the platform default Stripe account (backward compatible).
ALTER TABLE public.campaigns
  ADD COLUMN IF NOT EXISTS platform_stripe_account_id text;
