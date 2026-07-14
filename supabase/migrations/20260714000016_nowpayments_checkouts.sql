-- Pending NOWPayments checkouts (donor + amount) so IPN can record donations
-- even when the donor never hits the success return URL.
CREATE TABLE IF NOT EXISTS public.nowpayments_checkouts (
  payment_ref text PRIMARY KEY,
  invoice_id text,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS nowpayments_checkouts_invoice_id_idx
  ON public.nowpayments_checkouts (invoice_id)
  WHERE invoice_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS nowpayments_checkouts_created_at_idx
  ON public.nowpayments_checkouts (created_at DESC);
