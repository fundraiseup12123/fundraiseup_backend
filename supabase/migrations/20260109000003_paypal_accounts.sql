-- 3/7 PayPal connected accounts (org + campaign level)

create table if not exists public.paypal_accounts (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations(id) on delete cascade,
  campaign_id uuid references public.campaigns(id) on delete cascade,
  paypal_merchant_id text not null,
  paypal_email text,
  is_default boolean not null default false,
  connection_status text not null default 'pending'
    check (connection_status in ('pending', 'active', 'restricted', 'disabled')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (organization_id, paypal_merchant_id)
);

create index if not exists paypal_accounts_org_id_idx on public.paypal_accounts (organization_id);

alter table public.campaigns
  add column if not exists paypal_account_id uuid references public.paypal_accounts(id) on delete set null;
