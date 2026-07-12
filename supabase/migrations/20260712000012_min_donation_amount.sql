-- Minimum donation (in campaigns.default_currency). NULL = no limit.
alter table public.campaigns
  add column if not exists min_donation_amount numeric(12, 2) null;

comment on column public.campaigns.min_donation_amount is
  'Minimum gift in default_currency. NULL means no campaign minimum.';
