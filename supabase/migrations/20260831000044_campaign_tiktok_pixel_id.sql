-- TikTok Pixel ID per campaign (admin Content settings)
ALTER TABLE public.campaign_content
  ADD COLUMN IF NOT EXISTS tiktok_pixel_id text;
