-- Run this after you have created and confirmed your own Supabase user.
-- Replace the email before executing if your owner email is different.

update public.profiles
set billing_mode = 'at_cost',
    is_admin = true,
    monthly_cost_limit_usd = 25.00
where lower(email) = lower('m.singh@hufschmied.net');

select id, email, billing_mode, is_admin, monthly_cost_limit_usd
from public.profiles
where lower(email) = lower('m.singh@hufschmied.net');
