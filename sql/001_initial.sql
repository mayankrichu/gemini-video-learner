-- German Vocab Overlay v2
-- Run this entire file once in Supabase SQL Editor.

create extension if not exists pgcrypto;

do $$ begin
    create type public.billing_mode as enum ('prepaid', 'at_cost', 'free_admin');
exception when duplicate_object then null;
end $$;

do $$ begin
    create type public.entitlement_status as enum ('processing', 'ready', 'failed');
exception when duplicate_object then null;
end $$;

do $$ begin
    create type public.reservation_status as enum ('reserved', 'completed', 'released');
exception when duplicate_object then null;
end $$;

create table if not exists public.profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    email text,
    stripe_customer_id text unique,
    billing_mode public.billing_mode not null default 'prepaid',
    is_admin boolean not null default false,
    monthly_cost_limit_usd numeric(12, 2),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.credit_grants (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles(id) on delete cascade,
    granted_seconds bigint not null check (granted_seconds > 0),
    remaining_seconds bigint not null check (remaining_seconds >= 0),
    reserved_seconds bigint not null default 0 check (reserved_seconds >= 0),
    source text not null,
    stripe_checkout_session_id text unique,
    stripe_payment_intent_id text,
    revoked_at timestamptz,
    revoked_reason text,
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    check (reserved_seconds <= remaining_seconds)
);

create table if not exists public.ai_analyses (
    id uuid primary key default gen_random_uuid(),
    video_id text not null,
    transcript_hash text not null,
    settings_hash text not null,
    source_language text not null default 'de',
    target_language text not null default 'en',
    prompt_version text not null,
    model text not null,
    result jsonb not null,
    input_tokens bigint not null default 0,
    cached_input_tokens bigint not null default 0,
    output_tokens bigint not null default 0,
    provider_cost_usd numeric(16, 8) not null default 0,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (video_id, transcript_hash, settings_hash, prompt_version, model)
);

create table if not exists public.video_entitlements (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles(id) on delete cascade,
    video_id text not null,
    transcript_hash text not null,
    settings_hash text not null,
    request_id uuid not null,
    status public.entitlement_status not null default 'processing',
    analysis_id uuid references public.ai_analyses(id) on delete set null,
    charged_seconds bigint not null default 0 check (charged_seconds >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, video_id, transcript_hash, settings_hash)
);

create table if not exists public.usage_reservations (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles(id) on delete cascade,
    request_id uuid not null unique,
    video_id text not null,
    transcript_hash text not null,
    settings_hash text not null,
    reserved_seconds bigint not null check (reserved_seconds > 0),
    status public.reservation_status not null default 'reserved',
    expires_at timestamptz not null,
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.reservation_allocations (
    id uuid primary key default gen_random_uuid(),
    reservation_id uuid not null references public.usage_reservations(id) on delete cascade,
    credit_grant_id uuid not null references public.credit_grants(id) on delete cascade,
    reserved_seconds bigint not null check (reserved_seconds > 0),
    created_at timestamptz not null default now(),
    unique (reservation_id, credit_grant_id)
);

create table if not exists public.usage_events (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.profiles(id) on delete cascade,
    request_id uuid not null unique,
    video_id text not null,
    transcript_hash text not null,
    settings_hash text not null,
    analysis_id uuid references public.ai_analyses(id) on delete set null,
    billing_mode public.billing_mode not null default 'prepaid',
    billable_seconds bigint not null default 0 check (billable_seconds >= 0),
    charged_seconds bigint not null default 0 check (charged_seconds >= 0),
    model text,
    input_tokens bigint not null default 0,
    cached_input_tokens bigint not null default 0,
    output_tokens bigint not null default 0,
    provider_cost_usd numeric(16, 8) not null default 0,
    provider_cost_eur_estimate numeric(16, 8) not null default 0,
    pricing_snapshot jsonb not null default '{}'::jsonb,
    cache_hit boolean not null default false,
    reused_entitlement boolean not null default false,
    status text not null check (status in ('processing', 'completed', 'failed')),
    error_message text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.stripe_events (
    stripe_event_id text primary key,
    event_type text not null,
    processed_at timestamptz not null default now()
);

create index if not exists credit_grants_user_expiry_idx
    on public.credit_grants (user_id, expires_at);
create index if not exists credit_grants_available_idx
    on public.credit_grants (user_id, expires_at)
    where remaining_seconds > 0 and revoked_at is null;
create index if not exists credit_grants_payment_intent_idx
    on public.credit_grants (stripe_payment_intent_id)
    where stripe_payment_intent_id is not null;
create index if not exists entitlements_user_video_idx
    on public.video_entitlements (user_id, video_id);
create index if not exists analyses_video_hash_idx
    on public.ai_analyses (video_id, transcript_hash);
create index if not exists usage_events_user_month_idx
    on public.usage_events (user_id, created_at desc);
create index if not exists reservations_expiry_idx
    on public.usage_reservations (status, expires_at);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists profiles_set_updated_at on public.profiles;
create trigger profiles_set_updated_at
before update on public.profiles
for each row execute procedure public.set_updated_at();

drop trigger if exists analyses_set_updated_at on public.ai_analyses;
create trigger analyses_set_updated_at
before update on public.ai_analyses
for each row execute procedure public.set_updated_at();

drop trigger if exists entitlements_set_updated_at on public.video_entitlements;
create trigger entitlements_set_updated_at
before update on public.video_entitlements
for each row execute procedure public.set_updated_at();

drop trigger if exists reservations_set_updated_at on public.usage_reservations;
create trigger reservations_set_updated_at
before update on public.usage_reservations
for each row execute procedure public.set_updated_at();

drop trigger if exists usage_events_set_updated_at on public.usage_events;
create trigger usage_events_set_updated_at
before update on public.usage_events
for each row execute procedure public.set_updated_at();

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.profiles (id, email)
    values (new.id, new.email)
    on conflict (id) do update set email = excluded.email;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
after insert on auth.users
for each row execute procedure public.handle_new_user();

create or replace function public.release_credit_reservation(p_reservation_id uuid)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_reservation public.usage_reservations%rowtype;
    v_allocation record;
begin
    select * into v_reservation
    from public.usage_reservations
    where id = p_reservation_id
    for update;

    if not found then
        return false;
    end if;
    if v_reservation.status <> 'reserved' then
        return false;
    end if;

    for v_allocation in
        select *
        from public.reservation_allocations
        where reservation_id = p_reservation_id
    loop
        update public.credit_grants
        set reserved_seconds = greatest(0, reserved_seconds - v_allocation.reserved_seconds)
        where id = v_allocation.credit_grant_id;
    end loop;

    update public.usage_reservations
    set status = 'released', completed_at = now()
    where id = p_reservation_id;
    return true;
end;
$$;

create or replace function public.release_expired_credit_reservations()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    v_row record;
    v_count integer := 0;
begin
    for v_row in
        select id
        from public.usage_reservations
        where status = 'reserved' and expires_at <= now()
        for update skip locked
    loop
        if public.release_credit_reservation(v_row.id) then
            v_count := v_count + 1;
        end if;
    end loop;
    return v_count;
end;
$$;

create or replace function public.reserve_credits(
    p_user_id uuid,
    p_request_id uuid,
    p_video_id text,
    p_transcript_hash text,
    p_settings_hash text,
    p_required_seconds bigint,
    p_ttl_minutes integer
)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
    v_existing public.usage_reservations%rowtype;
    v_grant public.credit_grants%rowtype;
    v_total bigint := 0;
    v_needed bigint;
    v_available bigint;
    v_allocate bigint;
    v_reservation_id uuid;
    v_reservation_expires timestamptz := now() + make_interval(mins => p_ttl_minutes);
begin
    if p_required_seconds <= 0 then
        raise exception 'invalid_required_seconds';
    end if;

    perform public.release_expired_credit_reservations();

    select * into v_existing
    from public.usage_reservations
    where request_id = p_request_id
    for update;
    if found then
        if v_existing.user_id <> p_user_id then
            raise exception 'request_id_conflict';
        end if;
        if v_existing.status = 'reserved' then
            if v_existing.video_id <> p_video_id
               or v_existing.transcript_hash <> p_transcript_hash
               or v_existing.settings_hash <> p_settings_hash
               or v_existing.reserved_seconds <> p_required_seconds then
                raise exception 'request_id_conflict';
            end if;
            return v_existing.id;
        end if;
        raise exception 'request_already_finalized';
    end if;

    for v_grant in
        select *
        from public.credit_grants
        where user_id = p_user_id
          and revoked_at is null
          and expires_at > v_reservation_expires
          and remaining_seconds > reserved_seconds
        order by expires_at asc, created_at asc
        for update
    loop
        v_total := v_total + (v_grant.remaining_seconds - v_grant.reserved_seconds);
    end loop;

    if v_total < p_required_seconds then
        raise exception 'insufficient_credits';
    end if;

    insert into public.usage_reservations (
        user_id,
        request_id,
        video_id,
        transcript_hash,
        settings_hash,
        reserved_seconds,
        expires_at
    ) values (
        p_user_id,
        p_request_id,
        p_video_id,
        p_transcript_hash,
        p_settings_hash,
        p_required_seconds,
        v_reservation_expires
    ) returning id into v_reservation_id;

    v_needed := p_required_seconds;
    for v_grant in
        select *
        from public.credit_grants
        where user_id = p_user_id
          and revoked_at is null
          and expires_at > v_reservation_expires
          and remaining_seconds > reserved_seconds
        order by expires_at asc, created_at asc
        for update
    loop
        exit when v_needed <= 0;
        v_available := v_grant.remaining_seconds - v_grant.reserved_seconds;
        v_allocate := least(v_available, v_needed);
        if v_allocate <= 0 then
            continue;
        end if;

        update public.credit_grants
        set reserved_seconds = reserved_seconds + v_allocate
        where id = v_grant.id;

        insert into public.reservation_allocations (
            reservation_id,
            credit_grant_id,
            reserved_seconds
        ) values (
            v_reservation_id,
            v_grant.id,
            v_allocate
        );
        v_needed := v_needed - v_allocate;
    end loop;

    if v_needed <> 0 then
        raise exception 'credit_allocation_failed';
    end if;
    return v_reservation_id;
end;
$$;

create or replace function public.claim_video_entitlement(
    p_user_id uuid,
    p_video_id text,
    p_transcript_hash text,
    p_settings_hash text,
    p_request_id uuid,
    p_stale_after_seconds integer
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_row public.video_entitlements%rowtype;
begin
    insert into public.video_entitlements (
        user_id, video_id, transcript_hash, settings_hash, request_id, status
    ) values (
        p_user_id, p_video_id, p_transcript_hash, p_settings_hash, p_request_id, 'processing'
    )
    on conflict (user_id, video_id, transcript_hash, settings_hash) do nothing
    returning * into v_row;

    if found then
        return jsonb_build_object(
            'id', v_row.id,
            'status', v_row.status,
            'request_id', v_row.request_id,
            'analysis_id', v_row.analysis_id,
            'charged_seconds', v_row.charged_seconds,
            'claimed', true
        );
    end if;

    select * into v_row
    from public.video_entitlements
    where user_id = p_user_id
      and video_id = p_video_id
      and transcript_hash = p_transcript_hash
      and settings_hash = p_settings_hash
    for update;

    if v_row.status = 'ready' then
        return jsonb_build_object(
            'id', v_row.id,
            'status', v_row.status,
            'request_id', v_row.request_id,
            'analysis_id', v_row.analysis_id,
            'charged_seconds', v_row.charged_seconds,
            'claimed', false
        );
    end if;

    if v_row.status = 'failed'
       or v_row.updated_at <= now() - make_interval(secs => p_stale_after_seconds) then
        update public.video_entitlements
        set request_id = p_request_id,
            status = 'processing',
            analysis_id = null,
            charged_seconds = 0
        where id = v_row.id
        returning * into v_row;

        return jsonb_build_object(
            'id', v_row.id,
            'status', v_row.status,
            'request_id', v_row.request_id,
            'analysis_id', v_row.analysis_id,
            'charged_seconds', v_row.charged_seconds,
            'claimed', true
        );
    end if;

    return jsonb_build_object(
        'id', v_row.id,
        'status', v_row.status,
        'request_id', v_row.request_id,
        'analysis_id', v_row.analysis_id,
        'charged_seconds', v_row.charged_seconds,
        'claimed', false
    );
end;
$$;

create or replace function public.complete_video_entitlement(
    p_entitlement_id uuid,
    p_request_id uuid,
    p_analysis_id uuid,
    p_charged_seconds bigint
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
    update public.video_entitlements
    set status = 'ready',
        analysis_id = p_analysis_id,
        charged_seconds = p_charged_seconds
    where id = p_entitlement_id
      and request_id = p_request_id
      and status = 'processing';

    if not found then
        raise exception 'entitlement_not_owned';
    end if;
    return true;
end;
$$;

create or replace function public.fail_video_entitlement(
    p_entitlement_id uuid,
    p_request_id uuid
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
    update public.video_entitlements
    set status = 'failed'
    where id = p_entitlement_id
      and request_id = p_request_id
      and status = 'processing';
    return found;
end;
$$;

create or replace function public.finalize_analysis_purchase(
    p_reservation_id uuid,
    p_entitlement_id uuid,
    p_request_id uuid,
    p_analysis_id uuid,
    p_charged_seconds bigint
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_entitlement public.video_entitlements%rowtype;
    v_reservation public.usage_reservations%rowtype;
    v_allocation record;
begin
    select * into v_entitlement
    from public.video_entitlements
    where id = p_entitlement_id
    for update;
    if not found
       or v_entitlement.request_id <> p_request_id
       or v_entitlement.status <> 'processing' then
        raise exception 'entitlement_not_owned';
    end if;

    select * into v_reservation
    from public.usage_reservations
    where id = p_reservation_id
    for update;
    if not found
       or v_reservation.request_id <> p_request_id
       or v_reservation.status <> 'reserved' then
        raise exception 'reservation_not_active';
    end if;
    if v_reservation.user_id <> v_entitlement.user_id
       or v_reservation.video_id <> v_entitlement.video_id
       or v_reservation.transcript_hash <> v_entitlement.transcript_hash
       or v_reservation.settings_hash <> v_entitlement.settings_hash
       or v_reservation.reserved_seconds <> p_charged_seconds then
        raise exception 'reservation_entitlement_mismatch';
    end if;

    for v_allocation in
        select a.credit_grant_id, a.reserved_seconds
        from public.reservation_allocations a
        join public.credit_grants g on g.id = a.credit_grant_id
        where a.reservation_id = p_reservation_id
        for update of g
    loop
        update public.credit_grants
        set remaining_seconds = remaining_seconds - v_allocation.reserved_seconds,
            reserved_seconds = reserved_seconds - v_allocation.reserved_seconds
        where id = v_allocation.credit_grant_id
          and remaining_seconds >= v_allocation.reserved_seconds
          and reserved_seconds >= v_allocation.reserved_seconds;
        if not found then
            raise exception 'credit_finalize_failed';
        end if;
    end loop;

    update public.usage_reservations
    set status = 'completed', completed_at = now()
    where id = p_reservation_id;

    update public.video_entitlements
    set status = 'ready',
        analysis_id = p_analysis_id,
        charged_seconds = p_charged_seconds
    where id = p_entitlement_id;
    return true;
end;
$$;

create or replace function public.upsert_ai_analysis(
    p_video_id text,
    p_transcript_hash text,
    p_settings_hash text,
    p_source_language text,
    p_target_language text,
    p_prompt_version text,
    p_model text,
    p_result jsonb,
    p_input_tokens bigint,
    p_cached_input_tokens bigint,
    p_output_tokens bigint,
    p_provider_cost_usd numeric
)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
    v_id uuid;
begin
    insert into public.ai_analyses as existing (
        video_id,
        transcript_hash,
        settings_hash,
        source_language,
        target_language,
        prompt_version,
        model,
        result,
        input_tokens,
        cached_input_tokens,
        output_tokens,
        provider_cost_usd
    ) values (
        p_video_id,
        p_transcript_hash,
        p_settings_hash,
        p_source_language,
        p_target_language,
        p_prompt_version,
        p_model,
        p_result,
        p_input_tokens,
        p_cached_input_tokens,
        p_output_tokens,
        p_provider_cost_usd
    )
    on conflict (video_id, transcript_hash, settings_hash, prompt_version, model)
    do update set updated_at = existing.updated_at
    returning existing.id into v_id;
    return v_id;
end;
$$;

create or replace function public.get_account_snapshot(
    p_user_id uuid,
    p_usd_to_eur_rate numeric
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_profile public.profiles%rowtype;
    v_remaining bigint := 0;
    v_next_expiration timestamptz;
    v_month_cost numeric := 0;
    v_month_seconds numeric := 0;
    v_month_videos integer := 0;
begin
    perform public.release_expired_credit_reservations();

    select * into v_profile
    from public.profiles
    where id = p_user_id;
    if not found then
        raise exception 'profile_not_found';
    end if;

    select
        coalesce(sum(greatest(remaining_seconds - reserved_seconds, 0)), 0),
        min(expires_at) filter (
            where remaining_seconds > reserved_seconds and expires_at > now()
        )
    into v_remaining, v_next_expiration
    from public.credit_grants
    where user_id = p_user_id
      and revoked_at is null
      and expires_at > now();

    select
        coalesce(
            sum(provider_cost_usd) filter (
                where status in ('completed', 'failed')
            ),
            0
        ),
        coalesce(
            sum(billable_seconds) filter (where status = 'completed'),
            0
        ),
        count(distinct video_id) filter (where status = 'completed')
    into v_month_cost, v_month_seconds, v_month_videos
    from public.usage_events
    where user_id = p_user_id
      and created_at >= date_trunc('month', now());

    return jsonb_build_object(
        'user_id', v_profile.id,
        'email', v_profile.email,
        'billing_mode', v_profile.billing_mode,
        'is_admin', v_profile.is_admin,
        'remaining_seconds', case
            when v_profile.billing_mode = 'prepaid' then v_remaining
            else null
        end,
        'remaining_minutes', case
            when v_profile.billing_mode = 'prepaid' then floor(v_remaining / 60.0)::bigint
            else null
        end,
        'next_expiration', case
            when v_profile.billing_mode = 'prepaid' then v_next_expiration
            else null
        end,
        'current_month_provider_cost_usd', v_month_cost,
        'current_month_provider_cost_eur_estimate', v_month_cost * p_usd_to_eur_rate,
        'current_month_video_minutes', v_month_seconds / 60.0,
        'current_month_videos', v_month_videos,
        'monthly_cost_limit_usd', v_profile.monthly_cost_limit_usd
    );
end;
$$;

create or replace function public.grant_manual_credits(
    p_user_id uuid,
    p_seconds bigint,
    p_expiration_days integer,
    p_source text
)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
    v_id uuid;
begin
    if p_seconds <= 0 then
        raise exception 'invalid_credit_amount';
    end if;
    insert into public.credit_grants (
        user_id,
        granted_seconds,
        remaining_seconds,
        source,
        expires_at
    ) values (
        p_user_id,
        p_seconds,
        p_seconds,
        p_source,
        now() + make_interval(days => p_expiration_days)
    ) returning id into v_id;
    return v_id;
end;
$$;

create or replace function public.process_stripe_checkout(
    p_event_id text,
    p_event_type text,
    p_user_id uuid,
    p_seconds bigint,
    p_checkout_session_id text,
    p_payment_intent_id text,
    p_expiration_days integer
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_event_id text;
begin
    insert into public.stripe_events (stripe_event_id, event_type)
    values (p_event_id, p_event_type)
    on conflict (stripe_event_id) do nothing
    returning stripe_event_id into v_event_id;

    if v_event_id is null then
        return false;
    end if;

    insert into public.credit_grants (
        user_id,
        granted_seconds,
        remaining_seconds,
        source,
        stripe_checkout_session_id,
        stripe_payment_intent_id,
        expires_at
    ) values (
        p_user_id,
        p_seconds,
        p_seconds,
        'stripe',
        p_checkout_session_id,
        p_payment_intent_id,
        now() + make_interval(days => p_expiration_days)
    )
    on conflict (stripe_checkout_session_id) do nothing;
    return true;
end;
$$;

create or replace function public.process_stripe_credit_reversal(
    p_event_id text,
    p_event_type text,
    p_payment_intent_id text,
    p_reason text
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_event_id text;
begin
    insert into public.stripe_events (stripe_event_id, event_type)
    values (p_event_id, p_event_type)
    on conflict (stripe_event_id) do nothing
    returning stripe_event_id into v_event_id;

    if v_event_id is null then
        return false;
    end if;

    update public.credit_grants
    set remaining_seconds = reserved_seconds,
        revoked_at = coalesce(revoked_at, now()),
        revoked_reason = coalesce(revoked_reason, p_reason)
    where stripe_payment_intent_id = p_payment_intent_id
      and revoked_at is null;
    return true;
end;
$$;

alter table public.profiles enable row level security;
alter table public.credit_grants enable row level security;
alter table public.ai_analyses enable row level security;
alter table public.video_entitlements enable row level security;
alter table public.usage_reservations enable row level security;
alter table public.reservation_allocations enable row level security;
alter table public.usage_events enable row level security;
alter table public.stripe_events enable row level security;

-- The Chrome extension uses Supabase only for Auth. All billing/data access goes
-- through FastAPI with the server-side Supabase secret key.
revoke all on public.profiles from anon, authenticated;
revoke all on public.credit_grants from anon, authenticated;
revoke all on public.ai_analyses from anon, authenticated;
revoke all on public.video_entitlements from anon, authenticated;
revoke all on public.usage_reservations from anon, authenticated;
revoke all on public.reservation_allocations from anon, authenticated;
revoke all on public.usage_events from anon, authenticated;
revoke all on public.stripe_events from anon, authenticated;

grant all on public.profiles to service_role;
grant all on public.credit_grants to service_role;
grant all on public.ai_analyses to service_role;
grant all on public.video_entitlements to service_role;
grant all on public.usage_reservations to service_role;
grant all on public.reservation_allocations to service_role;
grant all on public.usage_events to service_role;
grant all on public.stripe_events to service_role;

revoke all on function public.release_credit_reservation(uuid) from public, anon, authenticated;
revoke all on function public.release_expired_credit_reservations() from public, anon, authenticated;
revoke all on function public.reserve_credits(uuid, uuid, text, text, text, bigint, integer) from public, anon, authenticated;
revoke all on function public.claim_video_entitlement(uuid, text, text, text, uuid, integer) from public, anon, authenticated;
revoke all on function public.complete_video_entitlement(uuid, uuid, uuid, bigint) from public, anon, authenticated;
revoke all on function public.fail_video_entitlement(uuid, uuid) from public, anon, authenticated;
revoke all on function public.finalize_analysis_purchase(uuid, uuid, uuid, uuid, bigint) from public, anon, authenticated;
revoke all on function public.upsert_ai_analysis(text, text, text, text, text, text, text, jsonb, bigint, bigint, bigint, numeric) from public, anon, authenticated;
revoke all on function public.get_account_snapshot(uuid, numeric) from public, anon, authenticated;
revoke all on function public.grant_manual_credits(uuid, bigint, integer, text) from public, anon, authenticated;
revoke all on function public.process_stripe_checkout(text, text, uuid, bigint, text, text, integer) from public, anon, authenticated;
revoke all on function public.process_stripe_credit_reversal(text, text, text, text) from public, anon, authenticated;

grant execute on function public.release_credit_reservation(uuid) to service_role;
grant execute on function public.release_expired_credit_reservations() to service_role;
grant execute on function public.reserve_credits(uuid, uuid, text, text, text, bigint, integer) to service_role;
grant execute on function public.claim_video_entitlement(uuid, text, text, text, uuid, integer) to service_role;
grant execute on function public.complete_video_entitlement(uuid, uuid, uuid, bigint) to service_role;
grant execute on function public.fail_video_entitlement(uuid, uuid) to service_role;
grant execute on function public.finalize_analysis_purchase(uuid, uuid, uuid, uuid, bigint) to service_role;
grant execute on function public.upsert_ai_analysis(text, text, text, text, text, text, text, jsonb, bigint, bigint, bigint, numeric) to service_role;
grant execute on function public.get_account_snapshot(uuid, numeric) to service_role;
grant execute on function public.grant_manual_credits(uuid, bigint, integer, text) to service_role;
grant execute on function public.process_stripe_checkout(text, text, uuid, bigint, text, text, integer) to service_role;
grant execute on function public.process_stripe_credit_reversal(text, text, text, text) to service_role;
