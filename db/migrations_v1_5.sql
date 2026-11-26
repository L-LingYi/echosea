-- EchoSea v1.9.2 Schema Migration (Idempotent)
-- 1. Track real SnowLuma gateway message_id on outbox_deliveries and per-bubble chat_messages.
-- 2. Track private DM peer_qq and internal request_id on chat_messages for reliable DM history & quotes.
-- 3. Default outbox_deliveries.feedback_status to 'none' and exclude pre-v1.9 legacy rows from cold_drop learning.

ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS peer_qq BIGINT,
    ADD COLUMN IF NOT EXISTS request_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS ix_chat_messages_private_peer_created
    ON chat_messages (scene, peer_qq, created_at);

UPDATE chat_messages
SET peer_qq = sender_qq
WHERE scene = 'private'
  AND peer_qq IS NULL
  AND gatekeeper_decision <> 'bot_reply';

ALTER TABLE outbox_deliveries
    ADD COLUMN IF NOT EXISTS onebot_message_id VARCHAR(64);

ALTER TABLE outbox_deliveries
    ALTER COLUMN feedback_status SET DEFAULT 'none';

-- Exclude pre-v1.9 legacy rows (missing gatekeeper_action) from being misclassified as cold_drop
UPDATE outbox_deliveries
SET feedback_status = 'none'
WHERE feedback_status = 'pending'
  AND gatekeeper_action IS NULL;
