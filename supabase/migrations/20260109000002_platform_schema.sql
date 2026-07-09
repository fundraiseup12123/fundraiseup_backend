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
