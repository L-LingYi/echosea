"""SQLAlchemy 2.0 ORM models compatible with PostgreSQL 17 (production) and SQLite (tests)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    totp_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class QQIdentity(Base):
    __tablename__ = "qq_identities"

    qq_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nickname: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    group_cards: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    known_names: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    relationship_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    public_profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    private_profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_scene_group_created", "scene", "group_id", "created_at"),
        Index("ix_chat_messages_sender_created", "sender_qq", "created_at"),
        Index("ix_chat_messages_msg_id", "message_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scene: Mapped[str] = mapped_column(String(16), nullable=False)  # group | private
    group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_qq: Mapped[int] = mapped_column(BigInteger, ForeignKey("qq_identities.qq_id", ondelete="CASCADE"), nullable=False)
    sender_display_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    expanded_reply_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    mentioned_qqs: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    media_meta: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    gatekeeper_decision: Mapped[str] = mapped_column(String(32), default="silent", nullable=False)
    gatekeeper_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_recalled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_ambient_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AmbientSummary(Base):
    __tablename__ = "ambient_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    active_participants: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    topics: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MemoryEpisode(Base):
    __tablename__ = "memory_episodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # public_group | private_dm | self
    group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    qq_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("qq_identities.qq_id", ondelete="CASCADE"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    is_anniversary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    embedding_json: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class GroupExpression(Base):
    __tablename__ = "group_expressions"
    __table_args__ = (UniqueConstraint("group_id", "phrase", name="uq_group_expression_phrase"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    phrase: Mapped[str] = mapped_column(String(128), nullable=False)
    meaning: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)  # pending | approved | rejected
    usage_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source_qq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SelfLifeState(Base):
    __tablename__ = "self_life_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    energy_level: Mapped[int] = mapped_column(Integer, default=80, nullable=False)
    mood: Mapped[str] = mapped_column(String(32), default="relaxed", nullable=False)
    current_activity: Mapped[str] = mapped_column(String(128), default="摸鱼看群", nullable=False)
    diary_entry: Mapped[str] = mapped_column(Text, default="", nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ModelProvider(Base):
    __tablename__ = "model_providers"
    __table_args__ = (UniqueConstraint("slot", "role", name="uq_model_provider_slot_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slot: Mapped[str] = mapped_column(String(32), nullable=False)  # main_brain | gatekeeper | reflection | embedding | tts | web_search
    role: Mapped[str] = mapped_column(String(16), default="primary", nullable=False)  # primary | fallback
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    protocol: Mapped[str] = mapped_column(String(32), default="openai_compatible", nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False)
    extra_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    rate_limit_rpm: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    price_input_per_1m: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    price_output_per_1m: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_health_status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    last_health_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class PersonaVersion(Base):
    __tablename__ = "persona_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_tag: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    bot_name: Mapped[str] = mapped_column(String(64), default="小呆呆", nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["呆呆", "小呆"], nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    few_shot_examples: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list, nullable=False)
    banned_phrases: Mapped[list[str]] = mapped_column(
        JSON,
        default=lambda: ["接住了", "记下了", "作为AI", "作为一个人工智能", "我理解你的感受", "希望这能帮到你"],
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SystemConfig(Base):
    __tablename__ = "system_configs"
    __table_args__ = (UniqueConstraint("scope_type", "scope_id", name="uq_system_config_scope"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)  # global | group | user
    scope_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class OutboxDelivery(Base):
    __tablename__ = "outbox_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scene: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reply_to_msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    onebot_status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    onebot_echo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UsageMetric(Base):
    __tablename__ = "usage_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slot: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    ttft_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(24), default="production", nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AlertChannel(Base):
    __tablename__ = "alert_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_type: Mapped[str] = mapped_column(String(32), nullable=False)  # smtp | webhook | qq_admin
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    config_enc: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)  # info | warning | critical
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_channels: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ImageVisionCache(Base):
    __tablename__ = "image_vision_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_gif_grid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StickerAsset(Base):
    __tablename__ = "sticker_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    emotion_tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    context_hint: Mapped[str] = mapped_column(Text, default="", nullable=False)
    appearance_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    positive_reactions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    origin: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)  # auto | manual
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)  # pending | approved | rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class TTSDelivery(Base):
    __tablename__ = "tts_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scene: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    intent: Mapped[str] = mapped_column(String(32), nullable=False)
    cleaned_text: Mapped[str] = mapped_column(Text, nullable=False)
    emotion: Mapped[str] = mapped_column(String(32), default="neutral", nullable=False)
    speed_ratio: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    synthesis_status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    onebot_status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)  # sent | fallback_text | failed
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    onebot_echo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class HotTopicSource(Base):
    __tablename__ = "hot_topic_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(32), default="general", nullable=False)
    feed_url: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DailyBriefing(Base):
    __tablename__ = "daily_briefings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    briefing_date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(32), default="general", nullable=False)
    digest_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class LifeScheduleSlot(Base):
    __tablename__ = "life_schedule_slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    start_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    end_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    activity: Mapped[str] = mapped_column(String(128), nullable=False)
    default_mood: Mapped[str] = mapped_column(String(32), default="relaxed", nullable=False)
    energy_target: Mapped[int] = mapped_column(Integer, default=75, nullable=False)
    is_sleeping: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class BackupRecord(Base):
    __tablename__ = "backup_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    remote_target: Mapped[str] = mapped_column(String(128), default="local", nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="success", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PipelineTrace(Base):
    """Privacy-safe per-stage trace sharing a unified request_id (no raw chat body logged)."""

    __tablename__ = "pipeline_traces"
    __table_args__ = (
        Index("ix_pipeline_traces_req", "request_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scene: Mapped[str] = mapped_column(String(16), nullable=False)
    group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)  # inbound | gatekeeper | main_brain | tts | onebot | recall
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
