-- EchoSea v1.6.0 - v1.8.0 Schema Migration (Idempotent)
ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 1;
