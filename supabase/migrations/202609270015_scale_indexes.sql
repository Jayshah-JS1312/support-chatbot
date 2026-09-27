-- Query-path indexes for many concurrent customer conversations.
-- All statements are additive and safe for rolling application deployment.

create index if not exists conversations_user_updated_idx
  on public.conversations(user_id, updated_at desc);

create index if not exists support_requests_conversation_active_idx
  on public.support_requests(conversation_id, created_at desc)
  where status in ('RECEIVED', 'QUEUED', 'DRAFTING', 'APPROVED', 'EXECUTING');

create index if not exists support_requests_user_status_created_idx
  on public.support_requests(user_id, status, created_at desc);

create index if not exists support_tickets_user_status_created_idx
  on public.support_tickets(user_id, status, created_at desc);
