-- Add missing donation fee columns (safe to re-run).
-- Run in Supabase SQL editor if donations fail to save after Stripe checkout.

alter table public.donations add column if not exists base_amount numeric(12, 2);

-- These may already exist from 001_platform_schema.sql; kept here for older databases.
alter table public.donations add column if not exists platform_fee numeric(12, 2);
alter table public.donations add column if not exists processing_fee numeric(12, 2);
alter table public.donations add column if not exists payout_amount numeric(12, 2);
alter table public.donations add column if not exists fee_covered boolean default false;
alter table public.donations add column if not exists utm jsonb;
alter table public.donations add column if not exists device jsonb;
alter table public.donations add column if not exists stripe_account_id text;
alter table public.donations add column if not exists organization_id uuid references public.organizations(id);
alter table public.donations add column if not exists campaign_id uuid references public.campaigns(id);
alter table public.donations add column if not exists status text not null default 'succeeded';

-- Backfill base_amount from amount for legacy rows.
update public.donations
set base_amount = amount
where base_amount is null;

-- Notify PostgREST to reload schema cache after migration.
notify pgrst, 'reload schema';
