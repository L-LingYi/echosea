-- Phase 2 & Phase 3 Extension Tables (appended to Companion System v1.0 Schema)

ALTER TABLE memory_episodes ADD COLUMN IF NOT EXISTS embedding_json JSONB;

-- 12. Multimodal Perceptual Hash Vision Cache
CREATE TABLE IF NOT EXISTS image_vision_cache (
    id              BIGSERIAL PRIMARY KEY,
    phash           VARCHAR(64) UNIQUE NOT NULL,
    source_url      TEXT NOT NULL DEFAULT '',
    is_gif_grid     BOOLEAN NOT NULL DEFAULT FALSE,
    description     TEXT NOT NULL DEFAULT '',
    hit_count       INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 13. Sticker Auto-Learning & Manual Gallery
CREATE TABLE IF NOT EXISTS sticker_assets (
    id                  BIGSERIAL PRIMARY KEY,
    phash               VARCHAR(64) UNIQUE NOT NULL,
    file_path           TEXT NOT NULL DEFAULT '',
    source_url          TEXT NOT NULL DEFAULT '',
    source_group_id     BIGINT,
    emotion_tags        JSONB NOT NULL DEFAULT '[]'::jsonb,
    context_hint        TEXT NOT NULL DEFAULT '',
    appearance_count    INTEGER NOT NULL DEFAULT 1,
    positive_reactions  INTEGER NOT NULL DEFAULT 0,
    origin              VARCHAR(16) NOT NULL DEFAULT 'auto' CHECK (origin IN ('auto', 'manual')),
    status              VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sticker_assets_status ON sticker_assets (status);

-- 14. Doubao TTS Voice Pipeline Delivery & Receipt Audit
CREATE TABLE IF NOT EXISTS tts_deliveries (
    id                  BIGSERIAL PRIMARY KEY,
    scene               VARCHAR(16) NOT NULL,
    target_id           BIGINT NOT NULL,
    intent              VARCHAR(32) NOT NULL,  -- answer_voice | read_quote | force_text
    cleaned_text        TEXT NOT NULL,
    emotion             VARCHAR(32) NOT NULL DEFAULT 'neutral',
    speed_ratio         DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    duration_seconds    DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    status              VARCHAR(24) NOT NULL DEFAULT 'pending', -- sent | fallback_text | failed
    fallback_reason     TEXT,
    onebot_echo         VARCHAR(64),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 15. Internet Hot-Topic Slow Channel Daily Briefings & Sources
CREATE TABLE IF NOT EXISTS hot_topic_sources (
    id              BIGSERIAL PRIMARY KEY,
    name            VARCHAR(128) NOT NULL,
    category        VARCHAR(32) NOT NULL DEFAULT 'general', -- hot | acg | tech | general
    feed_url        TEXT NOT NULL,
    is_enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS daily_briefings (
    id              BIGSERIAL PRIMARY KEY,
    briefing_date   DATE NOT NULL,
    category        VARCHAR(32) NOT NULL DEFAULT 'general',
    digest_text     TEXT NOT NULL,
    source_items    JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 16. Character Daily Schedule & Circadian State Machine Slots
CREATE TABLE IF NOT EXISTS life_schedule_slots (
    id              BIGSERIAL PRIMARY KEY,
    start_hour      INTEGER NOT NULL CHECK (start_hour BETWEEN 0 AND 23),
    end_hour        INTEGER NOT NULL CHECK (end_hour BETWEEN 0 AND 24),
    activity        VARCHAR(128) NOT NULL,
    default_mood    VARCHAR(32) NOT NULL DEFAULT 'relaxed',
    energy_target   INTEGER NOT NULL DEFAULT 75 CHECK (energy_target BETWEEN 0 AND 100),
    is_sleeping     BOOLEAN NOT NULL DEFAULT FALSE
);

-- 17. Remote Backup Audit Log
CREATE TABLE IF NOT EXISTS backup_records (
    id              BIGSERIAL PRIMARY KEY,
    filename        VARCHAR(256) NOT NULL,
    remote_target   VARCHAR(128) NOT NULL DEFAULT 'local',
    size_bytes      BIGINT NOT NULL DEFAULT 0,
    status          VARCHAR(24) NOT NULL DEFAULT 'success',
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
