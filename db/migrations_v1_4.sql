-- EchoSea v1.9.0 Schema Migration (Idempotent)
-- Adds social feedback tracking on outbox_deliveries, memory metabolism fields on memory_episodes,
-- and controlled self-evolution review queue (evolution_suggestions).

ALTER TABLE memory_episodes
    ADD COLUMN IF NOT EXISTS recall_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_recalled_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_memory_episodes_archived ON memory_episodes (is_archived, importance DESC);

ALTER TABLE outbox_deliveries
    ADD COLUMN IF NOT EXISTS feedback_status VARCHAR(24) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS feedback_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS gatekeeper_action VARCHAR(32),
    ADD COLUMN IF NOT EXISTS trigger_msg_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_outbox_feedback_lookup
    ON outbox_deliveries (scene, target_id, feedback_status, created_at DESC);

CREATE TABLE IF NOT EXISTS evolution_suggestions (
    id                  BIGSERIAL PRIMARY KEY,
    suggestion_type     VARCHAR(32) NOT NULL,
    status              VARCHAR(24) NOT NULL DEFAULT 'pending',
    group_id            BIGINT,
    source_request_id   VARCHAR(64),
    title               VARCHAR(200) NOT NULL DEFAULT '',
    content_json        JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason              TEXT NOT NULL DEFAULT '',
    expires_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_evolution_suggestions_type_status
    ON evolution_suggestions (suggestion_type, status, created_at DESC);
