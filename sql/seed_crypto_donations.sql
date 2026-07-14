-- Sample NOWPayments / crypto donations for admin list (run in Supabase SQL editor).
-- Safe to re-run: unique seed stripe_payment_intent_id values.

INSERT INTO public.donations (
  stripe_payment_intent_id,
  first_name,
  last_name,
  email,
  amount,
  base_amount,
  currency,
  frequency,
  payment_method,
  status,
  organization_id,
  campaign_id,
  platform_fee,
  processing_fee,
  payout_amount,
  fee_covered,
  crypto_amount,
  crypto_currency,
  comment,
  created_at
)
VALUES
  (
    'seed_np_001', 'Omar', 'Hassan', 'omar.hassan@example.com',
    12.00, 12.00, 'USD', 'once', 'nowpayments', 'succeeded',
    '00000000-0000-4000-8000-000000000001',
    '63fe73c9-d98a-42aa-baaa-65e3d26f8bf0',
    0, 0, 12.00, false,
    0.000112, 'BTC', 'Seed crypto donation (BTC)',
    now() - interval '20 minutes'
  ),
  (
    'seed_np_002', 'Layla', 'Noor', 'layla.noor@example.com',
    25.00, 25.00, 'USD', 'once', 'nowpayments', 'succeeded',
    '00000000-0000-4000-8000-000000000001',
    '63fe73c9-d98a-42aa-baaa-65e3d26f8bf0',
    0, 0, 25.00, false,
    0.0074, 'ETH', 'Seed crypto donation (ETH)',
    now() - interval '1 hour'
  ),
  (
    'seed_np_003', 'Daniel', 'Kim', 'daniel.kim@example.com',
    50.00, 50.00, 'USD', 'once', 'nowpayments', 'succeeded',
    '00000000-0000-4000-8000-000000000001',
    '63fe73c9-d98a-42aa-baaa-65e3d26f8bf0',
    0, 0, 50.00, false,
    0.00048, 'BTC', 'Seed crypto donation (BTC)',
    now() - interval '3 hours'
  ),
  (
    'seed_np_004', 'Fatima', 'Zahra', 'fatima.zahra@example.com',
    18.50, 18.50, 'USD', 'once', 'nowpayments', 'succeeded',
    '00000000-0000-4000-8000-000000000001',
    '63fe73c9-d98a-42aa-baaa-65e3d26f8bf0',
    0, 0, 18.50, false,
    52.25, 'USDT', 'Seed crypto donation (USDT)',
    now() - interval '5 hours'
  )
ON CONFLICT (stripe_payment_intent_id) DO UPDATE SET
  payment_method = EXCLUDED.payment_method,
  crypto_amount = EXCLUDED.crypto_amount,
  crypto_currency = EXCLUDED.crypto_currency,
  status = EXCLUDED.status;
