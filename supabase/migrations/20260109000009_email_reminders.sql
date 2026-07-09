-- Weekly email reminder subscriptions (popup + donor follow-ups)

create table if not exists public.email_reminders (
  id uuid primary key default gen_random_uuid(),
  email text not null,
  campaign_id uuid references public.campaigns(id) on delete cascade,
  organization_id uuid references public.organizations(id) on delete cascade,
  donor_name text,
  source text not null default 'popup' check (source in ('popup', 'donor')),
  active boolean not null default true,
  last_sent_at timestamptz,
  created_at timestamptz not null default now(),
  unique (email, campaign_id, source)
);

create index if not exists email_reminders_active_idx on public.email_reminders (active, last_sent_at);
create index if not exists email_logs_org_id_idx on public.email_logs (organization_id, sent_at desc);
create index if not exists email_logs_sent_at_idx on public.email_logs (sent_at desc);

alter table public.email_reminders enable row level security;

notify pgrst, 'reload schema';
