-- NOWPayments connected accounts (org and campaign level, mirrors paypal_accounts)
CREATE TABLE IF NOT EXISTS public.nowpayments_accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  campaign_id uuid REFERENCES public.campaigns(id) ON DELETE CASCADE,
  api_key text NOT NULL,
  ipn_secret text NOT NULL,
  api_key_hint text,
  email text,
  is_default boolean NOT NULL DEFAULT false,
  connection_status text NOT NULL DEFAULT 'active'
    CHECK (connection_status IN ('pending', 'active', 'restricted', 'disabled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS nowpayments_accounts_org_id_idx
  ON public.nowpayments_accounts (organization_id);

ALTER TABLE public.campaigns
  ADD COLUMN IF NOT EXISTS nowpayments_account_id uuid
    REFERENCES public.nowpayments_accounts(id) ON DELETE SET NULL;

ALTER TABLE public.nowpayments_accounts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Members read nowpayments accounts" ON public.nowpayments_accounts;
CREATE POLICY "Members read nowpayments accounts" ON public.nowpayments_accounts
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.organization_members m
      WHERE m.organization_id = nowpayments_accounts.organization_id
        AND m.user_id = auth.uid()
    )
  );
