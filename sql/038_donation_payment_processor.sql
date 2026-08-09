-- Tag each donation with the payment gateway that settled it.
-- Values: stripe | paypal | authorizenet_paypal | nowpayments

ALTER TABLE donations
  ADD COLUMN IF NOT EXISTS payment_processor text;

COMMENT ON COLUMN donations.payment_processor IS
  'Gateway that settled the gift: stripe, paypal, authorizenet_paypal, or nowpayments.';

CREATE INDEX IF NOT EXISTS donations_payment_processor_idx
  ON donations (payment_processor);

-- Backfill Authorize.net (explicit device tag)
UPDATE donations
SET payment_processor = 'authorizenet_paypal'
WHERE payment_processor IS NULL
  AND COALESCE(device->>'processor', '') = 'authorizenet';

-- Backfill crypto
UPDATE donations
SET payment_processor = 'nowpayments'
WHERE payment_processor IS NULL
  AND COALESCE(payment_method, '') = 'nowpayments';

-- Backfill Stripe PaymentIntents / Connect
UPDATE donations
SET payment_processor = 'stripe'
WHERE payment_processor IS NULL
  AND (
    COALESCE(stripe_payment_intent_id, '') LIKE 'pi_%'
    OR COALESCE(stripe_account_id, '') <> ''
  );

-- Remaining: prefer campaign processor, then org processor, else stripe
UPDATE donations d
SET payment_processor = COALESCE(
  NULLIF(LOWER(TRIM(c.payment_processor)), ''),
  NULLIF(LOWER(TRIM(o.payment_processor)), ''),
  'stripe'
)
FROM campaigns c
LEFT JOIN organizations o ON o.id = c.organization_id
WHERE d.campaign_id = c.id
  AND d.payment_processor IS NULL;
