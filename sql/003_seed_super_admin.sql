-- Super admin seed. Run in Supabase SQL editor AFTER creating the auth user.
--
-- 1. Supabase Dashboard → Authentication → Users → Add user
--    Email: test.superadmin@gmail.com
--    Password: 12345678
--    Auto confirm: yes
--
-- 2. Run this SQL to grant super_admin role (matches user by email):

update public.profiles
set role = 'super_admin',
    first_name = 'Test',
    last_name = 'Super Admin'
where id = (
  select id from auth.users where email = 'test.superadmin@gmail.com'
);

-- Verify (should return 1 row with role = super_admin):
-- select p.id, u.email, p.role from public.profiles p
-- join auth.users u on u.id = p.id
-- where u.email = 'test.superadmin@gmail.com';
