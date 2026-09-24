-- Chat lifecycle controls and fail-closed repair for interrupted workflows.

alter table public.conversations
  add column if not exists title text;

alter table public.conversations
  drop constraint if exists conversations_title_length_check,
  add constraint conversations_title_length_check check (
    title is null or char_length(btrim(title)) between 1 and 100
  );

-- Keep older databases compatible when a prior migration was recorded before
-- all approval-routing columns were applied.
alter table public.resolution_drafts
  add column if not exists requires_hitl boolean not null default true,
  add column if not exists routing_reason text;

-- A dead-letter is terminal and fail-closed. Repair requests left spinning by
-- the previous behavior; no privileged action is executed.
update public.support_requests
set status='COMPLETED_WITHOUT_ACTION', completed_at=coalesce(completed_at,now()),
    processing_lease_until=null, updated_at=now(),
    metadata=metadata || jsonb_build_object(
      'customer_status','Ami could not finish this response. No account action was taken. Please try again.',
      'failure_closed',true
    )
where status not in ('COMPLETED','COMPLETED_WITHOUT_ACTION')
  and last_error like 'workflow dead-letter:%';
