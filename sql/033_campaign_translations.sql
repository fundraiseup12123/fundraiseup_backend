-- Cached AI campaign story translations (instant public toggle after first warm).
CREATE TABLE IF NOT EXISTS public.campaign_translations (
  campaign_id uuid NOT NULL REFERENCES public.campaigns(id) ON DELETE CASCADE,
  lang text NOT NULL,
  content_fp text NOT NULL DEFAULT '',
  texts jsonb NOT NULL DEFAULT '{}'::jsonb,
  ui_strings jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (campaign_id, lang)
);

CREATE INDEX IF NOT EXISTS campaign_translations_lang_idx
  ON public.campaign_translations (lang);

COMMENT ON TABLE public.campaign_translations IS
  'Server-side cache of localized campaign story copy; keyed by campaign + language + content fingerprint.';
