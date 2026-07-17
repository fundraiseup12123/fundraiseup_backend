-- Organization timezone used for all admin-facing dates and times.
ALTER TABLE public.organizations
  ADD COLUMN IF NOT EXISTS timezone text NOT NULL DEFAULT 'UTC';
