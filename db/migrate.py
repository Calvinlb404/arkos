#!/usr/bin/env python3
"""
Migration runner for Arkos database schema.

Applies pending migrations from db/migrations/ if the tasks table doesn't exist.
Reads connection from DB_URL env var or constructs from POSTGRES_PASSWORD.
"""

import os
import sys
from pathlib import Path

import psycopg2


def get_connection_url():
    """Get Postgres connection URL from env or construct from POSTGRES_PASSWORD."""
    db_url = os.environ.get("DB_URL")
    if db_url:
        return db_url

    # Construct from POSTGRES_PASSWORD and defaults (matching docker-compose.yml)
    password = os.environ.get("POSTGRES_PASSWORD", "postgres")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    dbname = os.environ.get("POSTGRES_DB", "postgres")
    user = os.environ.get("POSTGRES_USER", "postgres")

    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


def table_exists(conn, table_name):
    """Check if a table exists in the database."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = %s AND table_schema = 'public'
            )
            """,
            (table_name,)
        )
        return cur.fetchone()[0]


def run_migration(conn, migration_path):
    """Read and execute a migration SQL file."""
    with open(migration_path) as f:
        sql = f.read()

    with conn.cursor() as cur:
        cur.execute(sql)

    conn.commit()


def main():
    """Main migration runner logic."""
    try:
        db_url = get_connection_url()
        conn = psycopg2.connect(db_url)

        # Check if tasks table already exists
        if table_exists(conn, "tasks"):
            print("✓ tasks table already exists. No migration needed.")
            conn.close()
            return 0

        # Table doesn't exist, apply migration
        migration_file = Path(__file__).parent / "migrations" / "0001_create_tasks_table.sql"

        if not migration_file.exists():
            print(f"✗ Migration file not found: {migration_file}", file=sys.stderr)
            conn.close()
            return 1

        print(f"Running migration: {migration_file.name}")
        run_migration(conn, migration_file)

        if table_exists(conn, "tasks"):
            print("✓ tasks table created successfully.")
            conn.close()
            return 0
        else:
            print("✗ Migration ran but tasks table was not created.", file=sys.stderr)
            conn.close()
            return 1

    except Exception as e:
        print(f"✗ Migration failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
