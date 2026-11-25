-- Companion System v1.1.0 Idempotent Live Upgrade Migration (PostgreSQL 17)
-- Safe to run on existing production databases with zero data loss.

-- 1. Fix schema drift observed in 2026-11-25 diagnostics (memory_episodes.embedding_json)
ALTER TABLE memory_episodes
    ADD COLUMN IF NOT EXISTS embedding_json JSONB;

-- 2. Tag usage_metrics.source ('production' | 'probe' | 'sandbox' | 'reflection')
--    so health probes never pollute production TTFT / P95 / cost stats
ALTER TABLE usage_metrics
    ADD COLUMN IF NOT EXISTS source VARCHAR(24) NOT NULL DEFAULT 'production',
    ADD COLUMN IF NOT EXISTS request_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_usage_metrics_source_time ON usage_metrics (source, created_at DESC);

-- 3. Separate TTS synthesis_status vs OneBot acceptance receipt + request_id correlation
ALTER TABLE tts_deliveries
    ADD COLUMN IF NOT EXISTS request_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS synthesis_status VARCHAR(24) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS onebot_status VARCHAR(24) NOT NULL DEFAULT 'pending';

-- The legacy TTS status 'sent' recorded synthesis, without a OneBot receipt.
UPDATE tts_deliveries
SET synthesis_status = CASE WHEN status = 'sent' THEN 'synthesized' ELSE 'fallback_text' END,
    onebot_status = CASE WHEN status = 'sent' THEN 'unverified' ELSE 'skipped' END
WHERE request_id IS NULL AND synthesis_status = 'pending' AND status IN ('sent', 'fallback_text');

-- 4. Correlate Outbox deliveries with request_id & OneBot receipt status
ALTER TABLE outbox_deliveries
    ADD COLUMN IF NOT EXISTS request_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS onebot_status VARCHAR(24) NOT NULL DEFAULT 'pending';

-- 5. Unified privacy-safe Pipeline Traces (inbound -> gatekeeper -> main_brain -> tts -> onebot)
--    Stores stage status, latency_ms, and error_type WITHOUT logging raw chat text.
CREATE TABLE IF NOT EXISTS pipeline_traces (
    id              BIGSERIAL PRIMARY KEY,
    request_id      VARCHAR(64) NOT NULL,
    scene           VARCHAR(16) NOT NULL,
    group_id        BIGINT,
    stage           VARCHAR(32) NOT NULL, -- inbound | gatekeeper | main_brain | tts | onebot | recall
    status          VARCHAR(32) NOT NULL, -- ok | silent | fallback | error | accepted | blocked
    latency_ms      INTEGER NOT NULL DEFAULT 0,
    error_type      VARCHAR(64),
    meta_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_traces_req_id ON pipeline_traces (request_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_pipeline_traces_time ON pipeline_traces (created_at DESC);
