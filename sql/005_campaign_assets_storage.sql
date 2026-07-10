-- Run once in Supabase SQL editor (Storage → campaign-assets bucket for uploaded logos/heroes/favicons).
insert into storage.buckets (id, name, public)
values ('campaign-assets', 'campaign-assets', true)
on conflict (id) do update set public = true;

drop policy if exists "Public read campaign assets" on storage.objects;
create policy "Public read campaign assets"
  on storage.objects for select
  using (bucket_id = 'campaign-assets');

drop policy if exists "Service role upload campaign assets" on storage.objects;
create policy "Service role upload campaign assets"
  on storage.objects for insert
  with check (bucket_id = 'campaign-assets');
