create table if not exists public.action_proposals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  action_name text not null check (action_name in ('cancel_order','start_return')),
  action_arguments jsonb not null,
  customer_consequences jsonb not null,
  policy_evidence jsonb not null,
  order_version integer not null check (order_version > 0),
  action_hash text not null check (action_hash ~ '^[0-9a-f]{64}$'),
  status text not null default 'previewed' check (status in ('previewed','confirmed','expired')),
  customer_confirmed_at timestamptz,
  expires_at timestamptz not null default (now() + interval '30 minutes'),
  created_at timestamptz not null default now()
);
create index if not exists action_proposals_owner_idx on public.action_proposals(user_id, created_at desc);

alter table public.resolution_drafts
  add column if not exists action_name text,
  add column if not exists action_arguments jsonb,
  add column if not exists customer_consequences jsonb,
  add column if not exists policy_evidence jsonb,
  add column if not exists order_version integer,
  add column if not exists action_hash text,
  add column if not exists customer_confirmed_at timestamptz;
alter table public.approval_tasks
  add column if not exists approved_action_hash text;

alter table public.action_proposals enable row level security;
grant select, insert, update on public.action_proposals to app_backend;
drop policy if exists app_access on public.action_proposals;
create policy app_access on public.action_proposals for all to app_backend
  using (app_private.is_admin() or user_id=app_private.current_user_id())
  with check (app_private.is_admin() or user_id=app_private.current_user_id());
