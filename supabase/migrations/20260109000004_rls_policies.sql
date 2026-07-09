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
