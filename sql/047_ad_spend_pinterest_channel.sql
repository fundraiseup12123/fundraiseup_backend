-- 047: Add 'pinterest' to ad_campaigns channel check constraint
alter table public.ad_campaigns drop constraint if exists ad_campaigns_channel_check;
alter table public.ad_campaigns add constraint ad_campaigns_channel_check check (channel in ('google', 'meta', 'tiktok', 'pinterest', 'other'));
