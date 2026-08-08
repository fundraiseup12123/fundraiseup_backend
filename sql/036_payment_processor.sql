-- Payment processor: stripe (default) or paypal (all card/wallet methods via PayPal keys).
-- NULL on campaigns = inherit organization. Existing rows stay Stripe behavior.
ALTER TABLE public.organizations
  ADD COLUMN IF NOT EXISTS payment_processor text
  NOT NULL DEFAULT 'stripe';

ALTER TABLE public.campaigns
  ADD COLUMN IF NOT EXISTS payment_processor text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'organizations_payment_processor_check'
  ) THEN
    ALTER TABLE public.organizations
      ADD CONSTRAINT organizations_payment_processor_check
      CHECK (payment_processor IN ('stripe', 'paypal'));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'campaigns_payment_processor_check'
  ) THEN
    ALTER TABLE public.campaigns
      ADD CONSTRAINT campaigns_payment_processor_check
      CHECK (payment_processor IS NULL OR payment_processor IN ('stripe', 'paypal'));
  END IF;
END $$;
