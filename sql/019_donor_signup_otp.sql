-- Pending donor signups awaiting email OTP verification
CREATE TABLE IF NOT EXISTS public.donor_signup_pending (
  email text PRIMARY KEY,
  full_name text NOT NULL,
  phone text,
  password_hash text NOT NULL,
  otp_hash text NOT NULL,
  attempts int NOT NULL DEFAULT 0,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.donor_signup_pending ENABLE ROW LEVEL SECURITY;
