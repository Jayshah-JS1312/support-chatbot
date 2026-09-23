-- Durable asynchronous support workflow, enqueue outbox, and deduplication.

alter table public.support_requests
  add column if not exists idempotency_key text,
  add column if not exists payload_hash text,
  add column if not exists workflow_run_id text,
  add column if not exists enqueue_attempts integer not null default 0,
  add column if not exists next_enqueue_at timestamptz not null default now(),
  add column if not exists enqueued_at timestamptz,
  add column if not exists processing_lease_until timestamptz,
  add column if not exists last_error text,
  add column if not exists completed_at timestamptz;

update public.support_requests
set status = 'COMPLETED', completed_at = coalesce(completed_at, updated_at)
where status not in (
  'RECEIVED','QUEUED','DRAFTING','AWAITING_APPROVAL','APPROVED',
  'EXECUTING','COMPLETED','REJECTED','EXPIRED','COMPLETED_WITHOUT_ACTION'
);

update public.support_requests
set idempotency_key = coalesce(idempotency_key, 'legacy:' || id::text),
    payload_hash = coalesce(payload_hash, encode(digest(summary, 'sha256'), 'hex'))
where idempotency_key is null or payload_hash is null;

alter table public.support_requests
  alter column idempotency_key set not null,
  alter column payload_hash set not null,
  drop constraint if exists support_requests_status_check,
  add constraint support_requests_status_check check (status in (
    'RECEIVED','QUEUED','DRAFTING','AWAITING_APPROVAL','APPROVED',
    'EXECUTING','COMPLETED','REJECTED','EXPIRED','COMPLETED_WITHOUT_ACTION'
  )),
  add constraint support_requests_enqueue_attempts_check check (enqueue_attempts >= 0);

create unique index if not exists support_requests_user_idempotency_key
  on public.support_requests(user_id, idempotency_key);
create index if not exists support_requests_recovery_idx
  on public.support_requests(status, next_enqueue_at)
  where enqueued_at is null or status in ('RECEIVED','APPROVED');

create unique index if not exists resolution_drafts_one_per_request
  on public.resolution_drafts(support_request_id);
create unique index if not exists approval_tasks_one_per_draft
  on public.approval_tasks(resolution_draft_id);

create table if not exists public.workflow_dead_letters (
  id bigint generated always as identity primary key,
  support_request_id uuid not null references public.support_requests(id) on delete cascade,
  workflow_run_id text,
  failure_status integer,
  failure_body text,
  failure_headers jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (support_request_id, workflow_run_id)
);

alter table public.workflow_dead_letters enable row level security;
grant select, insert, update on public.workflow_dead_letters to app_backend;
grant usage, select on sequence public.workflow_dead_letters_id_seq to app_backend;
drop policy if exists app_access on public.workflow_dead_letters;
create policy app_access on public.workflow_dead_letters for all to app_backend
  using (app_private.is_admin()) with check (app_private.is_admin());

grant select, insert, update on public.resolution_drafts, public.approval_tasks to app_backend;
