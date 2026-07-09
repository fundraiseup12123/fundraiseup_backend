-- Pop-up view branding (logo, hero, HTML copy for /pop-up-view + donation modal)
ALTER TABLE public.campaign_content
  ADD COLUMN IF NOT EXISTS popup_view_json text;