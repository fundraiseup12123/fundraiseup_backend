-- Donor portal: Stripe IDs for recurring management + editable donor profile

ALTER TABLE public.donations
  ADD COLUMN IF NOT EXISTS stripe_customer_id text,
  ADD COLUMN IF NOT EXISTS stripe_subscription_id text;

CREATE INDEX IF NOT EXISTS donations_email_lower_idx
  ON public.donations (lower(email));

CREATE INDEX IF NOT EXISTS donations_stripe_subscription_id_idx
  ON public.donations (stripe_subscription_id)
  WHERE stripe_subscription_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.donor_profiles (
  email text PRIMARY KEY,
  first_name text,
  last_name text,
  phone text,
  address_line1 text,
  address_line2 text,
  city text,
  region text,
  postal_code text,
  country text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.donor_profiles ENABLE ROW LEVEL SECURITY;
