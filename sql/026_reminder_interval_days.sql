-- Days between remind-me follow-up emails (popup X → Remind me later).
ALTER TABLE public.organizations
  ADD COLUMN IF NOT EXISTS reminder_interval_days integer NOT NULL DEFAULT 7;
