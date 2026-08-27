-- ============================================================================
-- Backfill payment_method & payment_processor for Previous PayPal Donations
-- ============================================================================

-- 1. Ensure all PayPal transactions have payment_processor = 'paypal'
UPDATE donations
SET payment_processor = 'paypal'
WHERE (payment_processor IS NULL OR payment_processor = '')
  AND (
    stripe_payment_intent_id LIKE 'paypal:%'
    OR stripe_payment_intent_id LIKE 'paypal-sub:%'
    OR stripe_payment_intent_id LIKE 'paypal-card:%'
    OR stripe_payment_intent_id LIKE 'paypal-apple:%'
    OR stripe_payment_intent_id LIKE 'paypal-google:%'
  );

-- 2. Update all previous PayPal card transactions so they show as "Card · PayPal"
-- (In past donations, payment_method was stored as 'paypal' by default;
-- this converts them to 'card' so the badge displays 💳 "Card · PayPal")
UPDATE donations
SET payment_method = 'card',
    payment_processor = 'paypal'
WHERE (payment_processor = 'paypal' OR stripe_payment_intent_id LIKE 'paypal:%')
  AND (payment_method = 'paypal' OR payment_method IS NULL OR payment_method = '');

-- 3. (Optional) If you have specific donations that were Apple Pay or Google Pay,
-- you can set their specific IDs here:
-- UPDATE donations SET payment_method = 'apple_pay', payment_processor = 'paypal' WHERE id = 'YOUR_DONATION_ID';
-- UPDATE donations SET payment_method = 'google_pay', payment_processor = 'paypal' WHERE id = 'YOUR_DONATION_ID';
