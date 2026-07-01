-- Run this in the Supabase SQL editor (Dashboard → SQL → New query).

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

drop policy if exists "Public read donations" on public.donations;
create policy "Public read donations"
  on public.donations
  for select
  using (true);
