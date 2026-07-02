-- Seed default organization and Sudan campaign from static site data.
-- Run after 001_platform_schema.sql and after creating a super admin user.

-- Example: create org (adjust name/slug as needed)
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
on conflict do nothing;

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
  hero_alt
)
values (
  '00000000-0000-4000-8000-000000000002',
  'Sudan Needs You - the world''s worst humanitarian crisis 😔💔',
  'More People Are in Famine in Sudan Than The Rest of The World Combined. 😔',
  '<p><strong>More People Are in Famine in Sudan Than The Rest of The World Combined.</strong></p>',
  'After completing your donation, you will see options to write a personalized message.',
  '#3872DC',
  '/assets/logo.avif',
  160,
  56,
  '/assets/herobanner.jfif',
  1248,
  702,
  'Save lives in Sudan'
)
on conflict (campaign_id) do update set
  title = excluded.title,
  caption = excluded.caption,
  primary_color = excluded.primary_color;

-- Link existing donations without campaign to default campaign (if donations table has new columns)
update public.donations
set
  organization_id = coalesce(organization_id, '00000000-0000-4000-8000-000000000001'),
  campaign_id = coalesce(campaign_id, '00000000-0000-4000-8000-000000000002')
where organization_id is null or campaign_id is null;
