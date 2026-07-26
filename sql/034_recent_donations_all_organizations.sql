-- Show donations from all organizations in this campaign's recent feed (default yes)
ALTER TABLE public.campaign_content
  ADD COLUMN IF NOT EXISTS recent_donations_all_organizations boolean NOT NULL DEFAULT true;
