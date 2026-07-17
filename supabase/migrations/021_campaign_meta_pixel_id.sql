-- Meta (Facebook) Pixel ID per campaign (admin Content settings)
ALTER TABLE public.campaign_content
  ADD COLUMN IF NOT EXISTS meta_pixel_id text;
