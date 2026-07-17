-- Landing page title/body font sizes (px); null = use CSS defaults
ALTER TABLE public.campaign_content
  ADD COLUMN IF NOT EXISTS title_font_size integer,
  ADD COLUMN IF NOT EXISTS body_font_size integer;
