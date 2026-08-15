-- Server-side recovery for approved PayPal redirects when browser storage is unavailable.
CREATE TABLE IF NOT EXISTS public.paypal_checkouts (
  payment_ref text PRIMARY KEY,
  order_id text,
  subscription_id text,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS paypal_checkouts_order_id_idx
  ON public.paypal_checkouts (order_id) WHERE order_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS paypal_checkouts_subscription_id_idx
  ON public.paypal_checkouts (subscription_id) WHERE subscription_id IS NOT NULL;

ALTER TABLE public.paypal_checkouts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.paypal_checkouts FROM anon, authenticated;
