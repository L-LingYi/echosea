-- Companion System v1.0 · PostgreSQL 17 + pgvector Initialization Schema
-- Covers: QQ Identity Binding, 5-Layer Memory (with Ambient Memory & Semi-Isolation),
--         Model Control Center (Encrypted Keys), Persona Versioning, 3-Layer Config,
--         Outbox Audit, Metrics, Alerts, and Admin Auth.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1. Admin Users & Security (WebUI Auth + optional TOTP 2FA)
CREATE TABLE IF NOT EXISTS admin_users (
    id              BIGSERIAL PRIMARY KEY,
    username        VARCHAR(64) UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    totp_secret_enc TEXT,
    totp_enabled    BOOLEAN NOT NULL DEFAULT FALSE,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. QQ Identity Strong Binding (QQ Number as Canonical Primary Key)
CREATE TABLE IF NOT EXISTS qq_identities (
    qq_id               BIGINT PRIMARY KEY,
    nickname            VARCHAR(128) NOT NULL DEFAULT '',
    group_cards         JSONB NOT NULL DEFAULT '{}'::jsonb,      -- {"group_id": "current_card"}
    known_names         JSONB NOT NULL DEFAULT '[]'::jsonb,      -- historical nicknames & cards
    relationship_level  INTEGER NOT NULL DEFAULT 0,              -- familiarity score
    public_profile      JSONB NOT NULL DEFAULT '{}'::jsonb,      -- visible in group chats
    private_profile     JSONB NOT NULL DEFAULT '{}'::jsonb,      -- STRICTLY DM-only (semi-isolation)
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_qq_identities_known_names ON qq_identities USING GIN (known_names);

-- 3. L1 Working Memory & L2 Ambient Silent Stream (Raw Messages)
CREATE TABLE IF NOT EXISTS chat_messages (
    id                  BIGSERIAL PRIMARY KEY,
    message_id          VARCHAR(64) NOT NULL,
    scene               VARCHAR(16) NOT NULL CHECK (scene IN ('group', 'private')),
    group_id            BIGINT,
    sender_qq           BIGINT NOT NULL REFERENCES qq_identities(qq_id) ON DELETE CASCADE,
    sender_display_name VARCHAR(128) NOT NULL DEFAULT '',
    raw_text            TEXT NOT NULL DEFAULT '',
    expanded_reply_text TEXT,                                    -- auto-expanded quoted message
    mentioned_qqs       JSONB NOT NULL DEFAULT '[]'::jsonb,
    media_meta          JSONB NOT NULL DEFAULT '[]'::jsonb,
    gatekeeper_decision VARCHAR(32) NOT NULL DEFAULT 'silent',   -- direct_pass | soft_entry | silent | blocked_recall
    gatekeeper_score    DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    is_recalled         BOOLEAN NOT NULL DEFAULT FALSE,
    is_ambient_only     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_scene_group_time ON chat_messages (scene, group_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_messages_sender_time ON chat_messages (sender_qq, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_messages_msg_id ON chat_messages (message_id);

-- 4. L2 Ambient Rolling Group Summaries (1-2h sliding window digests)
CREATE TABLE IF NOT EXISTS ambient_summaries (
    id                  BIGSERIAL PRIMARY KEY,
    group_id            BIGINT NOT NULL,
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,
    summary_text        TEXT NOT NULL,
    active_participants JSONB NOT NULL DEFAULT '[]'::jsonb,
    topics              JSONB NOT NULL DEFAULT '[]'::jsonb,
    message_count       INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ambient_summaries_group_time ON ambient_summaries (group_id, window_end DESC);

-- 5. L3 Episodic Memory (Shared Experiences, Milestones & Vector Search + FTS)
CREATE TABLE IF NOT EXISTS memory_episodes (
    id                  BIGSERIAL PRIMARY KEY,
    scope               VARCHAR(16) NOT NULL CHECK (scope IN ('public_group', 'private_dm', 'self')),
    group_id            BIGINT,
    qq_id               BIGINT REFERENCES qq_identities(qq_id) ON DELETE CASCADE,
    title               VARCHAR(256) NOT NULL DEFAULT '',
    content             TEXT NOT NULL,
    importance          DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    is_anniversary      BOOLEAN NOT NULL DEFAULT FALSE,
    event_date          DATE,
    embedding           vector(1536),
    embedding_json      JSONB,
    tsv                 tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(content, ''))) STORED,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memory_episodes_scope ON memory_episodes (scope, group_id, qq_id);
CREATE INDEX IF NOT EXISTS idx_memory_episodes_tsv ON memory_episodes USING GIN (tsv);

-- 6. L4 Group Slang & Expression Review Bank
CREATE TABLE IF NOT EXISTS group_expressions (
    id                  BIGSERIAL PRIMARY KEY,
    group_id            BIGINT NOT NULL,
    phrase              VARCHAR(128) NOT NULL,
    meaning             TEXT NOT NULL DEFAULT '',
    status              VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    usage_count         INTEGER NOT NULL DEFAULT 1,
    source_qq           BIGINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (group_id, phrase)
);

-- 7. L5 Self Life Memory (Energy, Mood, Schedule, Diary)
CREATE TABLE IF NOT EXISTS self_life_states (
    id                  BIGSERIAL PRIMARY KEY,
    energy_level        INTEGER NOT NULL DEFAULT 80 CHECK (energy_level BETWEEN 0 AND 100),
    mood                VARCHAR(32) NOT NULL DEFAULT 'relaxed',
    current_activity    VARCHAR(128) NOT NULL DEFAULT '摸鱼看群',
    diary_entry         TEXT NOT NULL DEFAULT '',
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 8. Model Control Center (6 Standard Slots, Primary + Fallback, Encrypted API Keys)
CREATE TABLE IF NOT EXISTS model_providers (
    id                  BIGSERIAL PRIMARY KEY,
    slot                VARCHAR(32) NOT NULL CHECK (slot IN ('main_brain', 'gatekeeper', 'reflection', 'embedding', 'tts', 'web_search')),
    role                VARCHAR(16) NOT NULL DEFAULT 'primary' CHECK (role IN ('primary', 'fallback')),
    name                VARCHAR(128) NOT NULL,
    protocol            VARCHAR(32) NOT NULL DEFAULT 'openai_compatible',
    base_url            TEXT NOT NULL,
    model_name          VARCHAR(128) NOT NULL,
    api_key_enc         TEXT NOT NULL,
    extra_config        JSONB NOT NULL DEFAULT '{}'::jsonb,
    rate_limit_rpm      INTEGER NOT NULL DEFAULT 60,
    timeout_seconds     INTEGER NOT NULL DEFAULT 30,
    price_input_per_1m  DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    price_output_per_1m DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    is_enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    last_health_status  VARCHAR(32) NOT NULL DEFAULT 'unknown',
    last_health_ms      INTEGER,
    last_checked_at     TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (slot, role)
);

-- 9. Persona Versions & 3-Layer Config Inheritance (global -> group -> user)
CREATE TABLE IF NOT EXISTS persona_versions (
    id                  BIGSERIAL PRIMARY KEY,
    version_tag         VARCHAR(64) UNIQUE NOT NULL,
    bot_name            VARCHAR(64) NOT NULL DEFAULT '小呆呆',
    aliases             JSONB NOT NULL DEFAULT '["呆呆", "小呆"]'::jsonb,
    system_prompt       TEXT NOT NULL,
    few_shot_examples   JSONB NOT NULL DEFAULT '[]'::jsonb,
    banned_phrases      JSONB NOT NULL DEFAULT '["接住了", "记下了", "作为AI", "作为一个人工智能", "我理解你的感受", "希望这能帮到你"]'::jsonb,
    is_active           BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS system_configs (
    id                  BIGSERIAL PRIMARY KEY,
    scope_type          VARCHAR(16) NOT NULL CHECK (scope_type IN ('global', 'group', 'user')),
    scope_id            VARCHAR(64) NOT NULL DEFAULT 'default',
    config_json         JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (scope_type, scope_id)
);

-- 10. Outbox Delivery Audit & Metrics Telemetry
CREATE TABLE IF NOT EXISTS outbox_deliveries (
    id                  BIGSERIAL PRIMARY KEY,
    scene               VARCHAR(16) NOT NULL,
    target_id           BIGINT NOT NULL,
    reply_to_msg_id     VARCHAR(64),
    payload_text        TEXT NOT NULL,
    status              VARCHAR(16) NOT NULL DEFAULT 'pending', -- pending | sent | failed | skipped_recall
    onebot_echo         VARCHAR(64),
    error_message       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at             TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS usage_metrics (
    id                  BIGSERIAL PRIMARY KEY,
    slot                VARCHAR(32) NOT NULL,
    provider_name       VARCHAR(128) NOT NULL,
    model_name          VARCHAR(128) NOT NULL,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    cached_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd  DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ttft_ms             INTEGER NOT NULL DEFAULT 0,
    total_ms            INTEGER NOT NULL DEFAULT 0,
    success             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_usage_metrics_time ON usage_metrics (created_at DESC);

-- 11. 24h Alerting Channels & History
CREATE TABLE IF NOT EXISTS alert_channels (
    id                  BIGSERIAL PRIMARY KEY,
    channel_type        VARCHAR(32) NOT NULL CHECK (channel_type IN ('smtp', 'webhook', 'qq_admin')),
    name                VARCHAR(128) NOT NULL,
    config_enc          TEXT NOT NULL,
    is_enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS alert_events (
    id                  BIGSERIAL PRIMARY KEY,
    severity            VARCHAR(16) NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    category            VARCHAR(64) NOT NULL,
    title               VARCHAR(256) NOT NULL,
    detail              TEXT NOT NULL,
    delivered_channels  JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
