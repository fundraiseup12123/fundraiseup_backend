-- Problem reports from checkout "Report a problem"
create table if not exists public.problem_reports (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid references public.organizations(id) on delete set null,
  campaign_id uuid references public.campaigns(id) on delete set null,
  description text not null,
  page_url text,
  user_agent text,
  created_at timestamptz not null default now()
);

create index if not exists problem_reports_org_idx
  on public.problem_reports (organization_id, created_at desc);

alter table public.problem_reports enable row level security;
