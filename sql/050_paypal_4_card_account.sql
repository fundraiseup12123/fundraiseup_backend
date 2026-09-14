-- PayPal 4: Dedicated card processing account for once and monthly card donations across all campaigns
INSERT INTO public.paypal_accounts (
  id,
  organization_id,
  paypal_merchant_id,
  paypal_email,
  is_default,
  connection_status,
  attach_mode,
  client_id,
  client_secret,
  client_id_hint
) VALUES (
  '44444444-4444-4000-8000-000000000004',
  '00000000-0000-4000-8000-000000000001',
  'PAYPAL_CARD_4',
  'paypal4-card@fundraiseup.local',
  false,
  'active',
  'keys',
  'BAAL1rFOZOTXdNniAIKrlMsZ2OETHTb2GFO2irV1kG55iinymWFLJr5g3UqT5F6tgVedQedJ3H-cTFzGPA',
  'EC9LHJlNNbe4IEg4VZ01zF-dpOU0GJmKYBcZsqOwA0IHRwbfqAba5c0FB6k8x3ApMsD59EaEgQHDlyoa',
  'BAAL1rFOZO'
)
ON CONFLICT (id) DO UPDATE SET
  client_id = EXCLUDED.client_id,
  client_secret = EXCLUDED.client_secret,
  client_id_hint = EXCLUDED.client_id_hint,
  connection_status = 'active',
  attach_mode = 'keys',
  updated_at = now();
