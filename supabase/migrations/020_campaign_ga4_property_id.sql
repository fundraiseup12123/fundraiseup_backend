-- Numeric GA4 property ID for Data API reporting (admins set alongside G- measurement ID)
ALTER TABLE public.campaign_content
  ADD COLUMN IF NOT EXISTS ga4_property_id text;
