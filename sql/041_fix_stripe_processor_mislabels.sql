-- Correct donations that settled via Stripe (PaymentIntent pi_...) but were
-- labeled Authorize.net because Stripe metadata inherited the campaign gateway.
-- Only updates payment_processor. Does not touch payment_method, amounts, or refs.
-- Safe against authorizenet:/paypal:/NOWPayments references (they are not pi_...).

-- Preview mismatches before applying:
-- SELECT
--   id,
--   created_at,
--   stripe_payment_intent_id,
--   payment_method,
--   payment_processor,
--   device->>'checkout_view' AS checkout_view
-- FROM public.donations
-- WHERE stripe_payment_intent_id ~ '^pi_[[:alnum:]_]+$'
--   AND (
--     payment_processor IS NULL
--     OR LOWER(BTRIM(payment_processor)) IN ('authorizenet', 'authorizenet_paypal')
--   )
-- ORDER BY created_at;

UPDATE public.donations
SET payment_processor = 'stripe'
WHERE stripe_payment_intent_id ~ '^pi_[[:alnum:]_]+$'
  AND (
    payment_processor IS NULL
    OR LOWER(BTRIM(payment_processor)) IN ('authorizenet', 'authorizenet_paypal')
  );

-- Verification: remaining pi_ rows labeled Authorize.net should be zero.
-- SELECT COUNT(*) AS remaining_mislabeled
-- FROM public.donations
-- WHERE stripe_payment_intent_id ~ '^pi_[[:alnum:]_]+$'
--   AND LOWER(BTRIM(COALESCE(payment_processor, '')))
--       IN ('authorizenet', 'authorizenet_paypal');

COMMENT ON COLUMN public.donations.payment_processor IS
  'Gateway that settled the gift: stripe, paypal, authorizenet_paypal, or nowpayments. Stripe PaymentIntents (pi_...) must be stripe.';
