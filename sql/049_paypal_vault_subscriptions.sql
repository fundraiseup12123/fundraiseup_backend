-- Table to track monthly recurring PayPal subscriptions
CREATE TABLE IF NOT EXISTS public.paypal_vault_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID,
    organization_id UUID,
    donor_email TEXT,
    donor_first_name TEXT,
    donor_last_name TEXT,
    payment_method VARCHAR(32) NOT NULL DEFAULT 'card',
    vault_token TEXT,
    amount NUMERIC(10, 2) NOT NULL,
    currency VARCHAR(8) NOT NULL,
    cover_fees BOOLEAN DEFAULT FALSE,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    billing_day_of_month INT,
    next_billing_date DATE NOT NULL,
    last_charge_date TIMESTAMPTZ DEFAULT now(),
    last_order_id TEXT,
    failure_count INT NOT NULL DEFAULT 0,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_paypal_vault_subs_status_due
    ON public.paypal_vault_subscriptions (status, next_billing_date);

ALTER TABLE public.paypal_vault_subscriptions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.paypal_vault_subscriptions FROM anon, authenticated;
