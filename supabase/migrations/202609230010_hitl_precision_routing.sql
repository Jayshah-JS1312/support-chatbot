alter table public.resolution_drafts
  add column if not exists requires_hitl boolean not null default true,
  add column if not exists routing_reason text;

alter table public.resolution_drafts
  drop constraint if exists resolution_drafts_status_check,
  add constraint resolution_drafts_status_check check (
    status in ('draft','pending_approval','approved','rejected','expired','completed')
  );

create index if not exists resolution_drafts_hitl_idx
  on public.resolution_drafts(requires_hitl, created_at desc);
