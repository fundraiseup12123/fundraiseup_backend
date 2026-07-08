-- PayPal connected accounts (org and campaign level, mirrors stripe_accounts)
CREATE TABLE IF NOT EXISTS public.paypal_accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  campaign_id uuid REFERENCES public.campaigns(id) ON DELETE CASCADE,
  paypal_merchant_id text NOT NULL,
  paypal_email text,
  is_default boolean NOT NULL DEFAULT false,
  connection_status text NOT NULL DEFAULT 'pending'
    CHECK (connection_status IN ('pending', 'active', 'restricted', 'disabled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, paypal_merchant_id)
);

CREATE INDEX IF NOT EXISTS paypal_accounts_org_id_idx ON public.paypal_accounts (organization_id);

ALTER TABLE public.campaigns
  ADD COLUMN IF NOT EXISTS paypal_account_id uuid REFERENCES public.paypal_accounts(id) ON DELETE SET NULL;
