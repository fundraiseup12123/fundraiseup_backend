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
