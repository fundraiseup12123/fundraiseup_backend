-- Crypto amounts received via NOWPayments (shown in admin alongside fiat gift)
ALTER TABLE public.donations
  ADD COLUMN IF NOT EXISTS crypto_amount numeric(24, 12),
  ADD COLUMN IF NOT EXISTS crypto_currency text;
