alter table public.approval_tasks
  add column if not exists review_necessary boolean,
  add column if not exists reassigned_at timestamptz;

create index if not exists approval_tasks_inbox_idx
  on public.approval_tasks(status, expires_at, created_at desc);

grant usage, select on sequence public.audit_events_id_seq to app_backend;
