-- Customers may create the approval task attached to their own sealed draft,
-- but only administrators may inspect or decide tasks in the human queue.
drop policy if exists app_access on public.approval_tasks;
drop policy if exists admin_access on public.approval_tasks;
drop policy if exists customer_insert on public.approval_tasks;

create policy admin_access on public.approval_tasks for all to app_backend
  using (app_private.is_admin())
  with check (app_private.is_admin());

create policy customer_insert on public.approval_tasks for insert to app_backend
  with check (exists(
    select 1
    from public.resolution_drafts d
    join public.support_requests r on r.id=d.support_request_id
    where d.id=resolution_draft_id
      and r.user_id=app_private.current_user_id()
      and d.customer_confirmed_at is not null
      and d.action_hash is not null
  ));
