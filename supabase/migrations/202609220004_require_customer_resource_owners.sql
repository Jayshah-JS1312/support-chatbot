-- Legacy Phase 2 rows without an authenticated owner become admin-owned and
-- therefore remain inaccessible to customers. New customer resources cannot
-- be written without an owner.
update public.conversations set user_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' where user_id is null;
update public.support_requests set user_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' where user_id is null;
update public.action_executions a set user_id=coalesce(
  (select o.user_id from public.orders o where o.id=a.order_id),
  (select c.user_id from public.conversations c where c.id=a.conversation_id),
  (select s.user_id from public.support_requests s where s.id=a.support_request_id),
  'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'::uuid
) where user_id is null;
update public.audit_events set user_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' where user_id is null;

alter table public.conversations alter column user_id set not null;
alter table public.support_requests alter column user_id set not null;
alter table public.action_executions alter column user_id set not null;
alter table public.audit_events alter column user_id set not null;
