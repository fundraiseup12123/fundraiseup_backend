-- Org-editable email template overrides + faster last-email lookup on donations

CREATE TABLE IF NOT EXISTS public.email_templates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  template_key text NOT NULL,
  subject text NOT NULL,
  headline text NOT NULL,
  body_html text NOT NULL DEFAULT '',
  banner_url text,
  logo_url text,
  cta_label text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (organization_id, template_key)
);

CREATE INDEX IF NOT EXISTS email_templates_org_id_idx
  ON public.email_templates (organization_id);

CREATE INDEX IF NOT EXISTS email_logs_donation_sent_at_idx
  ON public.email_logs (donation_id, sent_at DESC)
  WHERE donation_id IS NOT NULL;
