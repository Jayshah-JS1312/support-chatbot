create extension if not exists pgcrypto;

create sequence if not exists public.support_reference_seq start 1001;

create table if not exists public.profiles (
    id uuid primary key default gen_random_uuid(),
    auth_user_id uuid unique,
    email text not null unique check (email = lower(email)),
    display_name text not null,
    role text not null default 'customer' check (role in ('customer', 'admin', 'agent')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.orders (
    id uuid primary key default gen_random_uuid(),
    order_number text not null unique,
    profile_id uuid not null references public.profiles(id) on delete restrict,
    item_name text not null,
    price numeric(12,2) not null check (price >= 0),
    status text not null check (status in ('preparing', 'shipped', 'delivered', 'cancelled', 'return started', 'returned')),
    ordered_on date not null,
    delivered_on date,
    estimated_delivery date,
    carrier text,
    version integer not null default 1 check (version > 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check ((status in ('delivered', 'return started', 'returned') and delivered_on is not null)
        or status not in ('delivered', 'return started', 'returned'))
);

create table if not exists public.order_events (
    id bigint generated always as identity primary key,
    order_id uuid not null references public.orders(id) on delete cascade,
    event_type text not null,
    detail text not null,
    occurred_at timestamptz not null default now(),
    metadata jsonb not null default '{}'::jsonb
);
create index if not exists order_events_order_time_idx
    on public.order_events(order_id, occurred_at, id);

create table if not exists public.conversations (
    id uuid primary key default gen_random_uuid(),
    browser_session_id text not null unique,
    profile_id uuid references public.profiles(id) on delete set null,
    planner text not null default 'react' check (planner in ('react', 'plan')),
    working_memory jsonb not null default '{}'::jsonb,
    status text not null default 'open' check (status in ('open', 'closed', 'escalated')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.messages (
    id bigint generated always as identity primary key,
    conversation_id uuid not null references public.conversations(id) on delete cascade,
    sequence_number integer not null check (sequence_number >= 0),
    role text not null check (role in ('user', 'assistant', 'tool', 'system')),
    content text,
    payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    unique (conversation_id, sequence_number)
);
create index if not exists messages_conversation_idx
    on public.messages(conversation_id, sequence_number);

create table if not exists public.memory_facts (
    id uuid primary key default gen_random_uuid(),
    profile_id uuid not null references public.profiles(id) on delete cascade,
    conversation_id uuid references public.conversations(id) on delete set null,
    fact_type text not null,
    fact_key text not null,
    value jsonb not null,
    source text not null default 'verified_tool',
    valid_until timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (profile_id, fact_type, fact_key)
);
create index if not exists memory_facts_profile_idx on public.memory_facts(profile_id);

create table if not exists public.memory_summaries (
    profile_id uuid primary key references public.profiles(id) on delete cascade,
    summary jsonb not null default '{}'::jsonb,
    version integer not null default 1 check (version > 0),
    updated_at timestamptz not null default now()
);

create table if not exists public.support_requests (
    id uuid primary key default gen_random_uuid(),
    reference_number text not null unique,
    profile_id uuid references public.profiles(id) on delete set null,
    conversation_id uuid references public.conversations(id) on delete set null,
    order_id uuid references public.orders(id) on delete set null,
    request_type text not null,
    summary text not null,
    status text not null default 'open',
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.resolution_drafts (
    id uuid primary key default gen_random_uuid(),
    support_request_id uuid not null references public.support_requests(id) on delete cascade,
    content text not null,
    proposed_action jsonb,
    status text not null default 'draft' check (status in ('draft', 'pending_approval', 'approved', 'rejected', 'expired')),
    version integer not null default 1 check (version > 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.approval_tasks (
    id uuid primary key default gen_random_uuid(),
    resolution_draft_id uuid not null references public.resolution_drafts(id) on delete cascade,
    status text not null default 'pending' check (status in ('pending', 'approved', 'rejected', 'expired', 'cancelled')),
    assigned_to uuid references public.profiles(id) on delete set null,
    decision_reason text,
    expires_at timestamptz not null,
    decided_at timestamptz,
    created_at timestamptz not null default now()
);
create index if not exists approval_tasks_queue_idx on public.approval_tasks(status, expires_at);

create table if not exists public.action_executions (
    id uuid primary key default gen_random_uuid(),
    idempotency_key text not null unique,
    support_request_id uuid references public.support_requests(id) on delete set null,
    conversation_id uuid references public.conversations(id) on delete set null,
    order_id uuid references public.orders(id) on delete set null,
    action_type text not null,
    status text not null check (status in ('started', 'succeeded', 'failed', 'refused')),
    request_payload jsonb not null default '{}'::jsonb,
    result_payload jsonb not null default '{}'::jsonb,
    error_code text,
    started_at timestamptz not null default now(),
    finished_at timestamptz
);

create table if not exists public.audit_events (
    id bigint generated always as identity primary key,
    actor_type text not null,
    actor_id text,
    event_type text not null,
    resource_type text not null,
    resource_id text,
    request_id text,
    detail jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);
create index if not exists audit_events_created_idx on public.audit_events(created_at desc);

create table if not exists public.evaluation_runs (
    id uuid primary key default gen_random_uuid(),
    suite text not null,
    status text not null check (status in ('running', 'complete', 'failed')),
    summary jsonb,
    started_at timestamptz not null default now(),
    completed_at timestamptz
);

create table if not exists public.evaluation_results (
    id bigint generated always as identity primary key,
    evaluation_run_id uuid not null references public.evaluation_runs(id) on delete cascade,
    case_id text not null,
    passed boolean not null,
    rank integer,
    latency_ms numeric(12,3),
    detail jsonb not null default '{}'::jsonb,
    unique (evaluation_run_id, case_id)
);

do $$
declare table_name text;
begin
  foreach table_name in array array[
    'profiles', 'orders', 'order_events', 'conversations', 'messages',
    'memory_facts', 'memory_summaries', 'support_requests',
    'resolution_drafts', 'approval_tasks', 'action_executions',
    'audit_events', 'evaluation_runs', 'evaluation_results'
  ] loop
    execute format('alter table public.%I enable row level security', table_name);
    if exists (select 1 from pg_roles where rolname = 'anon') then
      execute format('revoke all on table public.%I from anon', table_name);
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
      execute format('revoke all on table public.%I from authenticated', table_name);
    end if;
  end loop;
end $$;

