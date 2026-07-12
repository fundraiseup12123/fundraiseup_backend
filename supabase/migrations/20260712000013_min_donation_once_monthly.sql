-- Separate minimums for one-time and monthly gifts (campaign default_currency).
alter table public.campaigns
  add column if not exists min_donation_amount_once numeric(12, 2) null;

alter table public.campaigns
  add column if not exists min_donation_amount_monthly numeric(12, 2) null;

comment on column public.campaigns.min_donation_amount_once is
  'Minimum one-time gift in default_currency. NULL means no once minimum.';

comment on column public.campaigns.min_donation_amount_monthly is
  'Minimum monthly gift in default_currency. NULL means no monthly minimum.';

-- Backfill from legacy single minimum when present.
update public.campaigns
set
  min_donation_amount_once = coalesce(min_donation_amount_once, min_donation_amount),
  min_donation_amount_monthly = coalesce(min_donation_amount_monthly, min_donation_amount)
where min_donation_amount is not null;
