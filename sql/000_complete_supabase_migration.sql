-- =============================================================================
-- COMPLETE SUPABASE MIGRATION — run once in Supabase SQL Editor (fresh or update)
-- Safe to re-run: uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS throughout.
--
-- Tables: donations, profiles, organizations, organization_members,
--         organization_invites, stripe_accounts, paypal_accounts, campaigns,
--         campaign_content (+ popup_view_json, payment_accounts_json),
--         campaign_currencies, domains, supporters, questions, tributes,
--         email_logs, exchange_rates
-- =============================================================================

-- 1/8 Base donations table (public feed + checkout records)

create table if not exists public.donations (
  id uuid primary key default gen_random_uuid(),
  stripe_payment_intent_id text unique,
  first_name text not null,
  last_name text not null,
  email text,
  amount numeric(12, 2) not null,
  currency text not null,
  frequency text not null default 'once' check (frequency in ('once', 'monthly')),
  payment_method text,
  honoree_name text,
  comment text,
  created_at timestamptz not null default now()
);

create index if not exists donations_created_at_idx on public.donations (created_at desc);

alter table public.donations enable row level security;
-- 2/7 Multi-tenant platform schema

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  role text not null default 'org_user' check (role in ('super_admin', 'org_user')),
  first_name text,
  last_name text,
  avatar_url text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.organizations (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  slug text not null unique,
  status text not null default 'active' check (status in ('active', 'suspended')),
  default_currency text not null default 'USD',
  reporting_currency text not null default 'USD',
  payment_methods jsonb not null default '{"card":true,"paypal":true,"google_pay":true,"apple_pay":true}'::jsonb,
  notification_prefs jsonb not null default '{}'::jsonb,
  created_by uuid references public.profiles(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.organization_members (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations(id) on delete cascade,
  user_id uuid not null references public.profiles(id) on delete cascade,
  role text not null default 'admin' check (role in ('owner', 'admin', 'member')),
  created_at timestamptz not null default now(),
  unique (organization_id, user_id)
);

create table if not exists public.organization_invites (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations(id) on delete cascade,
  email text not null,
  role text not null default 'admin' check (role in ('owner', 'admin', 'member')),
  token text not null unique default encode(gen_random_bytes(32), 'hex'),
  invited_by uuid references public.profiles(id),
  expires_at timestamptz not null default (now() + interval '7 days'),
  accepted_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists public.stripe_accounts (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations(id) on delete cascade,
  campaign_id uuid,
  stripe_account_id text not null,
  is_default boolean not null default false,
  connection_status text not null default 'pending' check (connection_status in ('pending', 'active', 'restricted', 'disabled')),
  charges_enabled boolean not null default false,
  payouts_enabled boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (stripe_account_id)
);

create table if not exists public.campaigns (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations(id) on delete cascade,
  slug text not null,
  name text not null,
  status text not null default 'draft' check (status in ('draft', 'live', 'archived')),
  default_currency text not null default 'USD',
  stripe_account_id uuid references public.stripe_accounts(id) on delete set null,
  designation text default 'General designation',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (organization_id, slug)
);

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'stripe_accounts_campaign_id_fkey'
  ) then
    alter table public.stripe_accounts
      add constraint stripe_accounts_campaign_id_fkey
      foreign key (campaign_id) references public.campaigns(id) on delete cascade;
  end if;
end $$;

create table if not exists public.campaign_content (
  campaign_id uuid primary key references public.campaigns(id) on delete cascade,
  title text not null,
  caption text,
  body_html text not null default '',
  dedication_hint text,
  primary_color text not null default '#3872DC',
  logo_url text,
  logo_width int not null default 160,
  logo_height int not null default 56,
  hero_url text,
  hero_width int not null default 1248,
  hero_height int not null default 702,
  hero_alt text,
  favicon_url text,
  popup_view_json text,
  payment_accounts_json text,
  updated_at timestamptz not null default now()
);

create table if not exists public.campaign_currencies (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid not null references public.campaigns(id) on delete cascade,
  currency_code text not null,
  enabled boolean not null default true,
  is_default boolean not null default false,
  amounts_once jsonb,
  amounts_monthly jsonb,
  unique (campaign_id, currency_code)
);

create table if not exists public.domains (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid not null references public.campaigns(id) on delete cascade,
  hostname text not null unique,
  verification_token text not null default encode(gen_random_bytes(16), 'hex'),
  verified_at timestamptz,
  ssl_status text not null default 'pending' check (ssl_status in ('pending', 'active', 'failed')),
  created_at timestamptz not null default now()
);

create table if not exists public.supporters (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references public.organizations(id) on delete cascade,
  email text,
  first_name text,
  last_name text,
  phone text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.questions (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid not null references public.campaigns(id) on delete cascade,
  label text not null,
  field_type text not null default 'text' check (field_type in ('text', 'textarea', 'select', 'checkbox')),
  options jsonb,
  required boolean not null default false,
  sort_order int not null default 0,
  created_at timestamptz not null default now()
);

create table if not exists public.tributes (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid not null references public.campaigns(id) on delete cascade,
  donation_id uuid,
  honoree_name text not null,
  message text,
  send_date timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists public.email_logs (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid references public.organizations(id) on delete cascade,
  donation_id uuid,
  recipient_email text not null,
  subject text not null,
  sent_at timestamptz not null default now(),
  opened_at timestamptz,
  template_key text
);

create table if not exists public.exchange_rates (
  id uuid primary key default gen_random_uuid(),
  base_currency text not null default 'USD',
  target_currency text not null,
  rate numeric(18, 8) not null,
  as_of date not null default current_date,
  unique (base_currency, target_currency, as_of)
);

-- Donations: multi-tenant + fee columns
alter table public.donations add column if not exists organization_id uuid references public.organizations(id);
alter table public.donations add column if not exists campaign_id uuid references public.campaigns(id);
alter table public.donations add column if not exists supporter_id uuid references public.supporters(id);
alter table public.donations add column if not exists status text not null default 'succeeded';
alter table public.donations add column if not exists base_amount numeric(12, 2);
alter table public.donations add column if not exists platform_fee numeric(12, 2);
alter table public.donations add column if not exists processing_fee numeric(12, 2);
alter table public.donations add column if not exists payout_amount numeric(12, 2);
alter table public.donations add column if not exists fee_covered boolean default false;
alter table public.donations add column if not exists utm jsonb;
alter table public.donations add column if not exists device jsonb;
alter table public.donations add column if not exists stripe_account_id text;

-- campaign_content extensions (safe if table already existed without them)
alter table public.campaign_content add column if not exists popup_view_json text;
alter table public.campaign_content add column if not exists payment_accounts_json text;

create index if not exists donations_campaign_id_idx on public.donations (campaign_id, created_at desc);
create index if not exists donations_org_id_idx on public.donations (organization_id, created_at desc);
create index if not exists campaigns_slug_idx on public.campaigns (slug);
create index if not exists domains_hostname_idx on public.domains (hostname);

create or replace function public.handle_new_user()
returns trigger as $$
begin
  insert into public.profiles (id, first_name, last_name)
  values (
    new.id,
    coalesce(new.raw_user_meta_data->>'first_name', ''),
    coalesce(new.raw_user_meta_data->>'last_name', '')
  );
  return new;
end;
$$ language plpgsql security definer;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();
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
-- 4/7 Row level security policies

alter table public.profiles enable row level security;
alter table public.organizations enable row level security;
alter table public.organization_members enable row level security;
alter table public.organization_invites enable row level security;
alter table public.campaigns enable row level security;
alter table public.campaign_content enable row level security;
alter table public.campaign_currencies enable row level security;
alter table public.domains enable row level security;
alter table public.stripe_accounts enable row level security;
alter table public.paypal_accounts enable row level security;
alter table public.supporters enable row level security;
alter table public.questions enable row level security;
alter table public.tributes enable row level security;
alter table public.email_logs enable row level security;

-- Public read live campaign data
drop policy if exists "Public read live campaigns" on public.campaigns;
create policy "Public read live campaigns" on public.campaigns
  for select using (status = 'live');

drop policy if exists "Public read campaign content" on public.campaign_content;
create policy "Public read campaign content" on public.campaign_content
  for select using (
    exists (select 1 from public.campaigns c where c.id = campaign_id and c.status = 'live')
  );

drop policy if exists "Public read campaign currencies" on public.campaign_currencies;
create policy "Public read campaign currencies" on public.campaign_currencies
  for select using (
    exists (select 1 from public.campaigns c where c.id = campaign_id and c.status = 'live')
  );

drop policy if exists "Public read verified domains" on public.domains;
create policy "Public read verified domains" on public.domains
  for select using (verified_at is not null);

drop policy if exists "Public read donations" on public.donations;
create policy "Public read donations" on public.donations
  for select using (
    campaign_id is null or exists (
      select 1 from public.campaigns c where c.id = campaign_id and c.status = 'live'
    )
  );

-- Profiles
drop policy if exists "Users read own profile" on public.profiles;
create policy "Users read own profile" on public.profiles
  for select using (id = auth.uid());

drop policy if exists "Users update own profile" on public.profiles;
create policy "Users update own profile" on public.profiles
  for update using (id = auth.uid());

-- Organization access for members + super admins
drop policy if exists "Members read own org" on public.organizations;
create policy "Members read own org" on public.organizations
  for select using (
    exists (
      select 1 from public.organization_members m
      where m.organization_id = id and m.user_id = auth.uid()
    )
    or exists (select 1 from public.profiles p where p.id = auth.uid() and p.role = 'super_admin')
  );

drop policy if exists "Members read own memberships" on public.organization_members;
create policy "Members read own memberships" on public.organization_members
  for select using (
    user_id = auth.uid()
    or exists (select 1 from public.profiles p where p.id = auth.uid() and p.role = 'super_admin')
  );

drop policy if exists "Members read campaigns" on public.campaigns;
create policy "Members read campaigns" on public.campaigns
  for select using (
    exists (
      select 1 from public.organization_members m
      where m.organization_id = organization_id and m.user_id = auth.uid()
    )
    or exists (select 1 from public.profiles p where p.id = auth.uid() and p.role = 'super_admin')
  );

drop policy if exists "Members manage campaigns" on public.campaigns;
create policy "Members manage campaigns" on public.campaigns
  for all using (
    exists (
      select 1 from public.organization_members m
      where m.organization_id = organization_id and m.user_id = auth.uid() and m.role in ('owner', 'admin')
    )
    or exists (select 1 from public.profiles p where p.id = auth.uid() and p.role = 'super_admin')
  );

drop policy if exists "Members read stripe accounts" on public.stripe_accounts;
create policy "Members read stripe accounts" on public.stripe_accounts
  for select using (
    exists (
      select 1 from public.organization_members m
      where m.organization_id = organization_id and m.user_id = auth.uid()
    )
    or exists (select 1 from public.profiles p where p.id = auth.uid() and p.role = 'super_admin')
  );

drop policy if exists "Members read paypal accounts" on public.paypal_accounts;
create policy "Members read paypal accounts" on public.paypal_accounts
  for select using (
    exists (
      select 1 from public.organization_members m
      where m.organization_id = organization_id and m.user_id = auth.uid()
    )
    or exists (select 1 from public.profiles p where p.id = auth.uid() and p.role = 'super_admin')
  );
-- 5/7 Seed default organization + Sudan root campaign (optional for fresh installs)

insert into public.organizations (id, name, slug, status, default_currency, reporting_currency)
values (
  '00000000-0000-4000-8000-000000000001',
  'Help Yateem',
  'help-yateem',
  'active',
  'PKR',
  'PKR'
)
on conflict (slug) do nothing;

insert into public.campaigns (id, organization_id, name, slug, status, default_currency)
values (
  '00000000-0000-4000-8000-000000000002',
  '00000000-0000-4000-8000-000000000001',
  'Sudan Needs You',
  'sudan',
  'live',
  'PKR'
)
on conflict (id) do nothing;

insert into public.campaign_content (
  campaign_id,
  title,
  caption,
  body_html,
  dedication_hint,
  primary_color,
  logo_url,
  logo_width,
  logo_height,
  hero_url,
  hero_width,
  hero_height,
  hero_alt,
  payment_accounts_json
)
values (
  '00000000-0000-4000-8000-000000000002',
  'Sudan Needs You - the world''s worst humanitarian crisis ðŸ˜”ðŸ’”',
  'More People Are in Famine in Sudan Than The Rest of The World Combined. ðŸ˜”',
  '<p><strong>More People Are in Famine in Sudan Than The Rest of The World Combined.</strong></p>',
  'After completing your donation, you will see options to write a personalized message.',
  '#3872DC',
  '/assets/logo.avif',
  160,
  56,
  '/assets/herobanner.jfif',
  1248,
  702,
  'Save lives in Sudan',
  '{"homepage":{"stripe_account_id":null,"stripe_connection_status":null,"stripe_charges_enabled":false,"paypal_merchant_id":null,"paypal_connection_status":null},"popup":{"stripe_account_id":null,"stripe_connection_status":null,"stripe_charges_enabled":false,"paypal_merchant_id":null,"paypal_connection_status":null}}'
)
on conflict (campaign_id) do update set
  title = excluded.title,
  caption = excluded.caption,
  primary_color = excluded.primary_color,
  payment_accounts_json = coalesce(public.campaign_content.payment_accounts_json, excluded.payment_accounts_json);

update public.donations
set
  organization_id = coalesce(organization_id, '00000000-0000-4000-8000-000000000001'),
  campaign_id = coalesce(campaign_id, '00000000-0000-4000-8000-000000000002'),
  base_amount = coalesce(base_amount, amount)
where organization_id is null or campaign_id is null or base_amount is null;
-- 6/7 Super admin role (run AFTER creating auth user in Supabase Dashboard)
--
-- Dashboard â†’ Authentication â†’ Users â†’ Add user
-- Then set email below and run:

-- update public.profiles
-- set role = 'super_admin',
--     first_name = 'Admin',
--     last_name = 'User'
-- where id = (
--   select id from auth.users where email = 'your@email.com'
-- );
-- 7/7 Notify PostgREST to reload schema cache

notify pgrst, 'reload schema';


-- Optional sample donations for the recent-donations feed (safe to re-run).

insert into public.donations (
  stripe_payment_intent_id,
  first_name,
  last_name,
  email,
  amount,
  currency,
  frequency,
  honoree_name,
  created_at
)
values
  ('seed_pi_001', 'FAIL', 'Ahmed', 'fail.ahmed@example.com', 5.00, 'USD', 'monthly', null, now() - interval '15 minutes'),
  ('seed_pi_002', 'Rwan', 'Ali', 'rwan.ali@example.com', 22.60, 'ILS', 'monthly', null, now() - interval '45 minutes'),
  ('seed_pi_003', 'Mohamed', 'Ibrahim', 'mohamed.i@example.com', 19.00, 'SAR', 'monthly', null, now() - interval '2 hours'),
  ('seed_pi_004', 'Suhair', 'Chaudhry', 'suhair.c@example.com', 13.50, 'NZD', 'monthly', null, now() - interval '4 hours'),
  ('seed_pi_005', 'Najda', 'Khan', 'najda.k@example.com', 16.70, 'BAM', 'monthly', 'Grandmother Fatima', now() - interval '6 hours'),
  ('seed_pi_006', 'Ayesha', 'Malik', 'ayesha.m@example.com', 15000.00, 'PKR', 'once', null, now() - interval '8 hours'),
  ('seed_pi_007', 'James', 'Miller', 'james.m@example.com', 50.00, 'EUR', 'once', null, now() - interval '12 hours'),
  ('seed_pi_008', 'Sarah', 'Nguyen', 'sarah.n@example.com', 75.00, 'AUD', 'monthly', null, now() - interval '18 hours'),
  ('seed_pi_009', 'Arjun', 'Patel', 'arjun.p@example.com', 4200.00, 'INR', 'once', null, now() - interval '1 day')
on conflict (stripe_payment_intent_id) do nothing;
