-- Companion System v1.2.0 Idempotent Migration (PostgreSQL 17)
-- Adds audited online update jobs table.

CREATE TABLE IF NOT EXISTS update_jobs (
    id              BIGSERIAL PRIMARY KEY,
    job_uid         VARCHAR(64) UNIQUE NOT NULL,
    triggered_by    VARCHAR(64) NOT NULL DEFAULT 'admin',
    mode            VARCHAR(24) NOT NULL DEFAULT 'local_repo', -- local_repo | github_public
    target_ref      VARCHAR(128) NOT NULL DEFAULT 'main',
    from_version    VARCHAR(64) NOT NULL DEFAULT '',
    from_commit     VARCHAR(64) NOT NULL DEFAULT '',
    to_commit       VARCHAR(64) NOT NULL DEFAULT '',
    status          VARCHAR(24) NOT NULL DEFAULT 'pending', -- pending | running | succeeded | rolled_back | failed
    rolled_back     BOOLEAN NOT NULL DEFAULT FALSE,
    log_summary     TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_update_jobs_created ON update_jobs (created_at DESC);
