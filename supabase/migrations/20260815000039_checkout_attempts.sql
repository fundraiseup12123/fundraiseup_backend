-- First-party, PII-free donation funnel telemetry.
CREATE TABLE IF NOT EXISTS public.checkout_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id text NOT NULL UNIQUE,
  session_id text NOT NULL,
  event_name text NOT NULL,
  campaign_id uuid,
  checkout_view text NOT NULL DEFAULT 'homepage'
    CHECK (checkout_view IN ('homepage', 'popup', 'landing')),
  funnel_step text,
  payment_method text,
  payment_processor text,
  frequency text CHECK (frequency IS NULL OR frequency IN ('once', 'monthly')),
  amount numeric,
  currency text,
  cover_fees boolean,
  transaction_id text,
  utm jsonb,
  device jsonb,
  metadata jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS checkout_attempts_session_created_idx
  ON public.checkout_attempts (session_id, created_at);
CREATE INDEX IF NOT EXISTS checkout_attempts_campaign_created_idx
  ON public.checkout_attempts (campaign_id, created_at DESC);
CREATE INDEX IF NOT EXISTS checkout_attempts_event_created_idx
  ON public.checkout_attempts (event_name, created_at DESC);
CREATE INDEX IF NOT EXISTS checkout_attempts_utm_campaign_idx
  ON public.checkout_attempts ((utm->>'campaign'));

ALTER TABLE public.checkout_attempts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.checkout_attempts FROM anon, authenticated;

COMMENT ON TABLE public.checkout_attempts IS
  'PII-free first-party donation funnel events, including abandoned checkout journeys.';
