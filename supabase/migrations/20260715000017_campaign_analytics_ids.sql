-- GA4 + GTM IDs per campaign (admin Content settings)
ALTER TABLE public.campaign_content
  ADD COLUMN IF NOT EXISTS ga4_measurement_id text,
  ADD COLUMN IF NOT EXISTS gtm_container_id text;
