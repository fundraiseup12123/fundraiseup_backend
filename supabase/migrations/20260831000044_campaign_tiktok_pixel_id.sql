-- TikTok Pixel ID per campaign (admin Content settings)
ALTER TABLE public.campaign_content
  ADD COLUMN IF NOT EXISTS tiktok_pixel_id text DEFAULT 'DAA859BC77U79TH2C8F0,DAA855RC77UEOA3OA1D0';

-- Backfill existing campaign content rows that have NULL tiktok_pixel_id
UPDATE public.campaign_content
SET tiktok_pixel_id = 'DAA859BC77U79TH2C8F0,DAA855RC77UEOA3OA1D0'
WHERE tiktok_pixel_id IS NULL OR tiktok_pixel_id = '';
