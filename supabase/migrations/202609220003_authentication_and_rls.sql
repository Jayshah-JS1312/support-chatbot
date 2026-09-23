do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'app_backend') then
    create role app_backend nologin;
  end if;
end $$;
grant app_backend to current_user;

alter table public.orders rename column profile_id to user_id;
alter table public.conversations rename column profile_id to user_id;
alter table public.memory_facts rename column profile_id to user_id;
alter table public.memory_summaries rename column profile_id to user_id;
alter table public.support_requests rename column profile_id to user_id;

alter table public.profiles add column if not exists password_hash text;
update public.profiles set password_hash = case email
  when 'raj@example.com' then '$2b$12$j13U2WsnUkP44jE7HB3R..FBQ5WIDLaEer7.byeDjvm.DQxuLxxs2'
  when 'mei@example.com' then '$2b$12$r.D51P.Rae2IpFCjg9igTOFGO1LqQ2f4raOGPOEifB3.myJzqObo6'
  else '$2b$12$j13U2WsnUkP44jE7HB3R..FBQ5WIDLaEer7.byeDjvm.DQxuLxxs2'
end where password_hash is null;
alter table public.profiles alter column password_hash set not null;

insert into public.profiles(id,email,display_name,role,password_hash)
values('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa','admin@example.com','Ami Admin','admin',
  '$2b$12$K48ubTO4gn8sQQagt0Ow6uDsGybOEpXMu7B4yvZPEq5bBvSvj2Ol6')
on conflict(email) do update set role='admin',password_hash=excluded.password_hash;

create table if not exists public.auth_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  token_hash text not null unique,
  expires_at timestamptz not null,
  revoked_at timestamptz,
  created_at timestamptz not null default now(),
  refreshed_at timestamptz
);
create index if not exists auth_sessions_active_idx on public.auth_sessions(token_hash,expires_at) where revoked_at is null;

create table if not exists public.password_reset_tokens (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  token_hash text not null unique,
  expires_at timestamptz not null,
  used_at timestamptz,
  created_at timestamptz not null default now()
);

alter table public.action_executions add column if not exists user_id uuid references public.profiles(id) on delete set null;
alter table public.audit_events add column if not exists user_id uuid references public.profiles(id) on delete set null;

create schema if not exists app_private;
revoke all on schema app_private from public;
grant usage on schema app_private to app_backend;

create or replace function app_private.current_user_id() returns uuid
language sql stable as $$
  select nullif(current_setting('app.current_user_id', true), '')::uuid
$$;
create or replace function app_private.is_admin() returns boolean
language sql stable as $$
  select current_setting('app.current_role', true) = 'admin'
$$;
grant execute on function app_private.current_user_id() to app_backend;
grant execute on function app_private.is_admin() to app_backend;

grant usage on schema public to app_backend;
grant select,insert,update,delete on all tables in schema public to app_backend;
grant usage,select on all sequences in schema public to app_backend;
alter default privileges in schema public grant select,insert,update,delete on tables to app_backend;
alter default privileges in schema public grant usage,select on sequences to app_backend;

do $$ declare t text; begin
  foreach t in array array[
    'profiles','orders','order_events','conversations','messages','memory_facts',
    'memory_summaries','support_requests','resolution_drafts','approval_tasks',
    'action_executions','audit_events','evaluation_runs','evaluation_results',
    'auth_sessions','password_reset_tokens'
  ] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('drop policy if exists app_access on public.%I', t);
  end loop;
end $$;

create policy app_access on public.profiles for all to app_backend
  using (app_private.is_admin() or id=app_private.current_user_id()
    or current_setting('app.auth_lookup',true)='on')
  with check (app_private.is_admin()
    or (id=app_private.current_user_id() and role='customer')
    or (current_setting('app.allow_signup',true)='on' and role='customer')
    or (current_setting('app.password_reset',true)='on' and role in ('customer','admin')));
create policy app_access on public.orders for all to app_backend
  using (app_private.is_admin() or user_id=app_private.current_user_id())
  with check (app_private.is_admin() or user_id=app_private.current_user_id());
create policy app_access on public.order_events for all to app_backend
  using (app_private.is_admin() or exists(select 1 from public.orders o where o.id=order_id and o.user_id=app_private.current_user_id()))
  with check (app_private.is_admin() or exists(select 1 from public.orders o where o.id=order_id and o.user_id=app_private.current_user_id()));
create policy app_access on public.conversations for all to app_backend
  using (app_private.is_admin() or user_id=app_private.current_user_id())
  with check (app_private.is_admin() or user_id=app_private.current_user_id());
create policy app_access on public.messages for all to app_backend
  using (app_private.is_admin() or exists(select 1 from public.conversations c where c.id=conversation_id and c.user_id=app_private.current_user_id()))
  with check (app_private.is_admin() or exists(select 1 from public.conversations c where c.id=conversation_id and c.user_id=app_private.current_user_id()));
create policy app_access on public.memory_facts for all to app_backend
  using (app_private.is_admin() or user_id=app_private.current_user_id())
  with check (app_private.is_admin() or user_id=app_private.current_user_id());
create policy app_access on public.memory_summaries for all to app_backend
  using (app_private.is_admin() or user_id=app_private.current_user_id())
  with check (app_private.is_admin() or user_id=app_private.current_user_id());
create policy app_access on public.support_requests for all to app_backend
  using (app_private.is_admin() or user_id=app_private.current_user_id())
  with check (app_private.is_admin() or user_id=app_private.current_user_id());
create policy app_access on public.resolution_drafts for all to app_backend
  using (app_private.is_admin() or exists(select 1 from public.support_requests s where s.id=support_request_id and s.user_id=app_private.current_user_id()))
  with check (app_private.is_admin() or exists(select 1 from public.support_requests s where s.id=support_request_id and s.user_id=app_private.current_user_id()));
create policy app_access on public.approval_tasks for all to app_backend
  using (app_private.is_admin()) with check (app_private.is_admin());
create policy app_access on public.action_executions for all to app_backend
  using (app_private.is_admin() or user_id=app_private.current_user_id())
  with check (app_private.is_admin() or user_id=app_private.current_user_id());
create policy app_access on public.audit_events for all to app_backend
  using (app_private.is_admin() or user_id=app_private.current_user_id())
  with check (app_private.is_admin() or user_id=app_private.current_user_id());
create policy app_access on public.evaluation_runs for all to app_backend
  using (app_private.is_admin()) with check (app_private.is_admin());
create policy app_access on public.evaluation_results for all to app_backend
  using (app_private.is_admin()) with check (app_private.is_admin());
create policy app_access on public.auth_sessions for all to app_backend
  using (app_private.is_admin() or user_id=app_private.current_user_id()
    or current_setting('app.auth_lookup',true)='on')
  with check (app_private.is_admin() or user_id=app_private.current_user_id()
    or current_setting('app.auth_lookup',true)='on');
create policy app_access on public.password_reset_tokens for all to app_backend
  using (app_private.is_admin() or user_id=app_private.current_user_id()
    or current_setting('app.auth_lookup',true)='on')
  with check (app_private.is_admin() or user_id=app_private.current_user_id()
    or current_setting('app.auth_lookup',true)='on');
