-- Per-provider checkout rail: platform (homepage) or organization Connect account.
ALTER TABLE public.organizations
  ADD COLUMN IF NOT EXISTS payment_account_sources jsonb
  NOT NULL DEFAULT '{"stripe":"organization","paypal":"organization","nowpayments":"organization"}'::jsonb;
