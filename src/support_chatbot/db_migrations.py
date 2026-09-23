"""Small migration runner for Docker and non-Supabase PostgreSQL deployments."""

import os
from pathlib import Path

import psycopg

from support_chatbot.config import settings


MIGRATIONS_DIR = Path(
    os.getenv("SUPPORT_CHATBOT_MIGRATIONS_DIR", Path.cwd() / "supabase" / "migrations")
)


def apply_migrations(database_url=None, migrations_dir=None):
    root = Path(migrations_dir or MIGRATIONS_DIR)
    files = sorted(root.glob("*.sql"))
    if not files:
        raise RuntimeError(f"No migrations found in {root}")
    with psycopg.connect(database_url or settings.database_url, autocommit=True) as conn:
        conn.execute("create schema if not exists app_migrations")
        conn.execute("""create table if not exists app_migrations.schema_migrations(
            version text primary key, applied_at timestamptz not null default now())""")
        conn.execute("select pg_advisory_lock(71522026)")
        try:
            applied = {
                row[0] for row in conn.execute(
                    "select version from app_migrations.schema_migrations"
                ).fetchall()
            }
            for migration in files:
                if migration.name in applied:
                    continue
                with conn.transaction():
                    conn.execute(migration.read_text(), prepare=False)
                    conn.execute(
                        "insert into app_migrations.schema_migrations(version) values(%s)",
                        (migration.name,),
                    )
                print(f"applied {migration.name}", flush=True)
        finally:
            conn.execute("select pg_advisory_unlock(71522026)")


def main():
    apply_migrations()


if __name__ == "__main__":
    main()
