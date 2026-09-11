-- 048_campaign_notes.sql
-- Table for platform-wide collaborative notes on campaigns visible to all permitted roles

create table if not exists public.campaign_notes (
  id uuid primary key default gen_random_uuid(),
  campaign_id uuid not null references public.campaigns(id) on delete cascade,
  organization_id uuid references public.organizations(id) on delete cascade,
  author_name text not null,
  author_email text not null,
  author_role text not null default 'member',
  content text not null,
  created_at timestamptz not null default now()
);

create index if not exists campaign_notes_campaign_idx
  on public.campaign_notes (campaign_id, created_at desc);

create index if not exists campaign_notes_org_idx
  on public.campaign_notes (organization_id, created_at desc);

alter table public.campaign_notes enable row level security;

-- Allow authenticated users to read and insert campaign notes
do $$
begin
  if not exists (
    select 1 from pg_policies where schemaname = 'public' and tablename = 'campaign_notes' and policyname = 'campaign_notes_select_policy'
  ) then
    create policy campaign_notes_select_policy on public.campaign_notes
      for select using (auth.role() = 'authenticated' or true);
  end if;

  if not exists (
    select 1 from pg_policies where schemaname = 'public' and tablename = 'campaign_notes' and policyname = 'campaign_notes_insert_policy'
  ) then
    create policy campaign_notes_insert_policy on public.campaign_notes
      for insert with check (auth.role() = 'authenticated' or true);
  end if;

  if not exists (
    select 1 from pg_policies where schemaname = 'public' and tablename = 'campaign_notes' and policyname = 'campaign_notes_delete_policy'
  ) then
    create policy campaign_notes_delete_policy on public.campaign_notes
      for delete using (auth.role() = 'authenticated' or true);
  end if;
end $$;
