-- Optional display name shown in outbound emails. NULL/empty = use organizations.name.
ALTER TABLE public.organizations
  ADD COLUMN IF NOT EXISTS email_organization_name text;
