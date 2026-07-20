-- Allow platform_admin profile role (all-org org console, payment methods read-only).
ALTER TABLE public.profiles DROP CONSTRAINT IF EXISTS profiles_role_check;
ALTER TABLE public.profiles
  ADD CONSTRAINT profiles_role_check
  CHECK (role IN ('super_admin', 'platform_admin', 'org_user'));
