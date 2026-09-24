-- Human support handoffs are tickets, not workflow runs or approval tasks.

create table if not exists public.support_tickets (
  id uuid primary key default gen_random_uuid(),
  reference_number text not null unique,
  user_id uuid not null references public.profiles(id) on delete cascade,
  conversation_id uuid references public.conversations(id) on delete set null,
  source_request_id uuid references public.support_requests(id) on delete set null,
  summary text not null,
  status text not null default 'open'
    check (status in ('open','in_progress','resolved','closed')),
  resolution text,
  assigned_to uuid references public.profiles(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  resolved_at timestamptz
);

create index if not exists support_tickets_customer_time_idx
  on public.support_tickets(user_id, created_at desc);
create index if not exists support_tickets_queue_idx
  on public.support_tickets(status, created_at);

-- Preserve handoffs created by the earlier implementation.
insert into public.support_tickets
  (reference_number,user_id,conversation_id,summary,status,created_at,updated_at,resolved_at)
select reference_number,user_id,conversation_id,summary,'open',created_at,updated_at,null
from public.support_requests
where request_type='escalation'
on conflict(reference_number) do nothing;

alter table public.support_tickets enable row level security;
grant select,insert,update on public.support_tickets to app_backend;
drop policy if exists app_access on public.support_tickets;
create policy app_access on public.support_tickets for all to app_backend
  using (app_private.is_admin() or user_id=app_private.current_user_id())
  with check (app_private.is_admin() or user_id=app_private.current_user_id());
