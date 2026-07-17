-- Formatted visual campaign title; plain title remains canonical for non-visual uses
ALTER TABLE public.campaign_content
  ADD COLUMN IF NOT EXISTS title_html text;
