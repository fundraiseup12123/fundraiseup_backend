-- 046: Ad Spend & ROI Tracking Tables
-- Supports Google Ads, Meta Ads, and TikTok Ads with effective-date budget history.

create table if not exists public.ad_campaigns (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  channel text not null check (channel in ('google', 'meta', 'tiktok', 'pinterest', 'other')),
  utm_campaign text,
  utm_source text,
  campaign_id uuid references public.campaigns(id) on delete set null,
  status text not null default 'active' check (status in ('active', 'paused', 'completed')),
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.ad_campaign_budgets (
  id uuid primary key default gen_random_uuid(),
  ad_campaign_id uuid not null references public.ad_campaigns(id) on delete cascade,
  daily_budget numeric(12, 2) not null check (daily_budget >= 0),
  currency text not null default 'USD',
  start_date date not null,
  end_date date,
  created_at timestamptz not null default now()
);

create index if not exists idx_ad_campaigns_channel on public.ad_campaigns(channel);
create index if not exists idx_ad_campaigns_status on public.ad_campaigns(status);
create index if not exists idx_ad_campaigns_utm_campaign on public.ad_campaigns(utm_campaign);
create index if not exists idx_ad_campaign_budgets_campaign on public.ad_campaign_budgets(ad_campaign_id);
create index if not exists idx_ad_campaign_budgets_dates on public.ad_campaign_budgets(start_date, end_date);

alter table public.ad_campaigns enable row level security;
alter table public.ad_campaign_budgets enable row level security;

create policy "Allow service_role full access on ad_campaigns"
  on public.ad_campaigns for all
  using (true)
  with check (true);

create policy "Allow service_role full access on ad_campaign_budgets"
  on public.ad_campaign_budgets for all
  using (true)
  with check (true);
