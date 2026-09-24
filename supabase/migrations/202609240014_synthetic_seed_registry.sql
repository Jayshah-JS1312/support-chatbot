-- Registry for explicit, development-only synthetic datasets. Bulk test data is
-- never inserted by a migration; the guarded CLI records successful runs here.
create table if not exists public.synthetic_seed_runs (
  dataset_id text primary key,
  generator_version integer not null check (generator_version > 0),
  record_counts jsonb not null,
  content_fingerprint text not null check (content_fingerprint ~ '^[0-9a-f]{64}$'),
  generated_at timestamptz not null,
  check (dataset_id ~ '^synthetic-dev-[a-z0-9-]+$')
);

alter table public.synthetic_seed_runs enable row level security;
grant select, insert, update, delete on public.synthetic_seed_runs to app_backend;
drop policy if exists admin_access on public.synthetic_seed_runs;
create policy admin_access on public.synthetic_seed_runs for all to app_backend
  using (app_private.is_admin()) with check (app_private.is_admin());

-- These indexes keep the common customer and operator queries responsive when
-- the optional 10k-record development dataset is installed.
create index if not exists orders_user_created_idx
  on public.orders(user_id, created_at desc);
create index if not exists support_requests_user_created_idx
  on public.support_requests(user_id, created_at desc);

