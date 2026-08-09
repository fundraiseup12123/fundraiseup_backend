-- Third payment processor: authorizenet_paypal (card/Apple/PayPal via Authorize.net; Google Pay via PayPal keys).
-- Org can attach own Authorize.net keys or use platform homepage keys via payment_account_sources.authorizenet.

ALTER TABLE public.organizations
  DROP CONSTRAINT IF EXISTS organizations_payment_processor_check;

ALTER TABLE public.campaigns
  DROP CONSTRAINT IF EXISTS campaigns_payment_processor_check;

ALTER TABLE public.organizations
  ADD CONSTRAINT organizations_payment_processor_check
  CHECK (payment_processor IN ('stripe', 'paypal', 'authorizenet_paypal'));

ALTER TABLE public.campaigns
  ADD CONSTRAINT campaigns_payment_processor_check
  CHECK (payment_processor IS NULL OR payment_processor IN ('stripe', 'paypal', 'authorizenet_paypal'));

CREATE TABLE IF NOT EXISTS public.authorizenet_accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  campaign_id uuid REFERENCES public.campaigns(id) ON DELETE CASCADE,
  api_login_id text NOT NULL,
  transaction_key text NOT NULL,
  signature_key text,
  public_client_key text NOT NULL,
  api_login_id_hint text,
  public_client_key_hint text,
  env text NOT NULL DEFAULT 'production'
    CHECK (env IN ('sandbox', 'production')),
  is_default boolean NOT NULL DEFAULT false,
  connection_status text NOT NULL DEFAULT 'active'
    CHECK (connection_status IN ('pending', 'active', 'restricted', 'disabled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, api_login_id)
);

CREATE INDEX IF NOT EXISTS authorizenet_accounts_org_id_idx
  ON public.authorizenet_accounts (organization_id);

ALTER TABLE public.campaigns
  ADD COLUMN IF NOT EXISTS authorizenet_account_id uuid
    REFERENCES public.authorizenet_accounts(id) ON DELETE SET NULL;

ALTER TABLE public.authorizenet_accounts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Members read authorizenet accounts" ON public.authorizenet_accounts;
CREATE POLICY "Members read authorizenet accounts" ON public.authorizenet_accounts
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.organization_members m
      WHERE m.organization_id = authorizenet_accounts.organization_id
        AND m.user_id = auth.uid()
    )
  );

-- Ensure payment_account_sources JSON includes authorizenet for existing orgs/campaigns.
UPDATE public.organizations
SET payment_account_sources =
  COALESCE(payment_account_sources, '{}'::jsonb) || '{"authorizenet":"organization"}'::jsonb
WHERE payment_account_sources IS NULL
   OR NOT (payment_account_sources ? 'authorizenet');

UPDATE public.campaigns
SET payment_account_sources =
  COALESCE(payment_account_sources, '{}'::jsonb) || '{"authorizenet":"organization"}'::jsonb
WHERE payment_account_sources IS NOT NULL
  AND NOT (payment_account_sources ? 'authorizenet');
