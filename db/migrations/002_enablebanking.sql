-- 002: Enable Banking (replaces GoCardless, which closed to new signups Jul 2025)

ALTER TYPE provider_kind ADD VALUE IF NOT EXISTS 'enablebanking';

ALTER TABLE provider_connection
    ADD COLUMN IF NOT EXISTS session_id TEXT,
    ADD COLUMN IF NOT EXISTS aspsp_name TEXT,
    ADD COLUMN IF NOT EXISTS aspsp_country TEXT;

-- Short-lived state tokens for the bank authorization redirect flow
CREATE TABLE IF NOT EXISTS banking_auth_state (
    state       TEXT PRIMARY KEY,
    aspsp_name  TEXT NOT NULL,
    aspsp_country TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
