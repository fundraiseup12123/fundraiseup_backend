-- Backfill payment_method and payment_processor for previous and new PayPal / gateway donations.

-- 1. Ensure PayPal processor is tagged on paypal order / subscription rows
UPDATE donations
SET payment_processor = 'paypal'
WHERE (payment_processor IS NULL OR payment_processor = '')
  AND (
    stripe_payment_intent_id LIKE 'paypal:%'
    OR stripe_payment_intent_id LIKE 'paypal-sub:%'
    OR stripe_payment_intent_id LIKE 'paypal-card:%'
  );

-- 2. If device or intent explicitly noted card/apple/google on paypal donations, update payment_method
UPDATE donations
SET payment_method = 'card'
WHERE (payment_method IS NULL OR payment_method = '' OR payment_method = 'paypal')
  AND (
    LOWER(COALESCE(device->>'payment_method', '')) = 'card'
    OR LOWER(COALESCE(device->>'method', '')) = 'card'
    OR stripe_payment_intent_id LIKE 'paypal-card:%'
  );

UPDATE donations
SET payment_method = 'apple_pay'
WHERE (payment_method IS NULL OR payment_method = '' OR payment_method = 'paypal')
  AND (
    LOWER(COALESCE(device->>'payment_method', '')) = 'apple_pay'
    OR LOWER(COALESCE(device->>'method', '')) = 'apple_pay'
    OR stripe_payment_intent_id LIKE 'paypal-apple:%'
  );

UPDATE donations
SET payment_method = 'google_pay'
WHERE (payment_method IS NULL OR payment_method = '' OR payment_method = 'paypal')
  AND (
    LOWER(COALESCE(device->>'payment_method', '')) = 'google_pay'
    OR LOWER(COALESCE(device->>'method', '')) = 'google_pay'
    OR stripe_payment_intent_id LIKE 'paypal-google:%'
  );

-- 3. Default any other empty payment_method on stripe to 'card'
UPDATE donations
SET payment_method = 'card'
WHERE (payment_method IS NULL OR payment_method = '')
  AND payment_processor = 'stripe';
