-- Recent donations display options on campaign pages
ALTER TABLE public.campaign_content
  ADD COLUMN IF NOT EXISTS show_donor_country boolean NOT NULL DEFAULT false;

ALTER TABLE public.campaign_content
  ADD COLUMN IF NOT EXISTS recent_donations_sort text NOT NULL DEFAULT 'recent';

ALTER TABLE public.campaign_content
  DROP CONSTRAINT IF EXISTS campaign_content_recent_donations_sort_check;

ALTER TABLE public.campaign_content
  ADD CONSTRAINT campaign_content_recent_donations_sort_check
  CHECK (recent_donations_sort IN ('recent', 'descending'));
