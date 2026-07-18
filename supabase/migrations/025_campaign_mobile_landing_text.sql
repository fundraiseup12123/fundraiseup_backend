-- Optional mobile overrides for landing page copy (desktop fields stay default)
ALTER TABLE public.campaign_content
  ADD COLUMN IF NOT EXISTS title_html_mobile text,
  ADD COLUMN IF NOT EXISTS body_html_mobile text,
  ADD COLUMN IF NOT EXISTS caption_mobile text,
  ADD COLUMN IF NOT EXISTS title_font_size_mobile integer,
  ADD COLUMN IF NOT EXISTS body_font_size_mobile integer;
