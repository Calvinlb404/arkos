-- Migration: 0002_create_users_table
-- Minimal users table for demo-style auth (username only, no password).
-- Swappable for Supabase GoTrue or email+password later without breaking tasks.user_id (UUID).

BEGIN;

CREATE TABLE IF NOT EXISTS users (
    id          UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    username    VARCHAR(128)    NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ     NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);

COMMIT;
