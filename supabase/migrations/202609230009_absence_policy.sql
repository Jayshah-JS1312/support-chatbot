-- Durable, fail-closed human-absence lifecycle.

alter table public.approval_tasks
  add column if not exists reminder_at timestamptz,
  add column if not exists reminded_at timestamptz,
  add column if not exists escalation_at timestamptz,
  add column if not exists escalated_at timestamptz,
  add column if not exists queue_name text not null default 'review';

update public.approval_tasks
set reminder_at = coalesce(reminder_at, created_at + interval '1 hour'),
    escalation_at = coalesce(escalation_at, created_at + interval '2 hours')
where reminder_at is null or escalation_at is null;

alter table public.approval_tasks
  alter column reminder_at set not null,
  alter column escalation_at set not null,
  add constraint approval_task_deadline_order check (
    reminder_at < escalation_at and escalation_at < expires_at
  );

create index if not exists approval_tasks_absence_due_idx
  on public.approval_tasks(status, reminder_at, escalation_at, expires_at)
  where status = 'pending';
