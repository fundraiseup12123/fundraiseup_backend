-- Minimum donation (in campaigns.default_currency). NULL = no limit.
alter table public.campaigns
  add column if not exists min_donation_amount numeric(12, 2) null;
