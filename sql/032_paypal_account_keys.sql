-- Optional PayPal Client ID / Secret attach mode (existing email accounts stay email).
ALTER TABLE public.paypal_accounts
  ADD COLUMN IF NOT EXISTS attach_mode text NOT NULL DEFAULT 'email';

ALTER TABLE public.paypal_accounts
  ADD COLUMN IF NOT EXISTS client_id text;

ALTER TABLE public.paypal_accounts
  ADD COLUMN IF NOT EXISTS client_secret text;

ALTER TABLE public.paypal_accounts
  ADD COLUMN IF NOT EXISTS client_id_hint text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'paypal_accounts_attach_mode_check'
  ) THEN
    ALTER TABLE public.paypal_accounts
      ADD CONSTRAINT paypal_accounts_attach_mode_check
      CHECK (attach_mode IN ('email', 'keys'));
  END IF;
END $$;
