-- Optional sample donations for the recent-donations feed (safe to re-run).

insert into public.donations (
  stripe_payment_intent_id,
  first_name,
  last_name,
  email,
  amount,
  currency,
  frequency,
  honoree_name,
  created_at
)
values
  ('seed_pi_001', 'FAIL', 'Ahmed', 'fail.ahmed@example.com', 5.00, 'USD', 'monthly', null, now() - interval '15 minutes'),
  ('seed_pi_002', 'Rwan', 'Ali', 'rwan.ali@example.com', 22.60, 'ILS', 'monthly', null, now() - interval '45 minutes'),
  ('seed_pi_003', 'Mohamed', 'Ibrahim', 'mohamed.i@example.com', 19.00, 'SAR', 'monthly', null, now() - interval '2 hours'),
  ('seed_pi_004', 'Suhair', 'Chaudhry', 'suhair.c@example.com', 13.50, 'NZD', 'monthly', null, now() - interval '4 hours'),
  ('seed_pi_005', 'Najda', 'Khan', 'najda.k@example.com', 16.70, 'BAM', 'monthly', 'Grandmother Fatima', now() - interval '6 hours'),
  ('seed_pi_006', 'Ayesha', 'Malik', 'ayesha.m@example.com', 15000.00, 'PKR', 'once', null, now() - interval '8 hours'),
  ('seed_pi_007', 'James', 'Miller', 'james.m@example.com', 50.00, 'EUR', 'once', null, now() - interval '12 hours'),
  ('seed_pi_008', 'Sarah', 'Nguyen', 'sarah.n@example.com', 75.00, 'AUD', 'monthly', null, now() - interval '18 hours'),
  ('seed_pi_009', 'Arjun', 'Patel', 'arjun.p@example.com', 4200.00, 'INR', 'once', null, now() - interval '1 day')
on conflict (stripe_payment_intent_id) do nothing;
