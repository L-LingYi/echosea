"""Pydantic schemas for normalized OneBot v11 envelopes, Gatekeeper decisions, and Admin API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# --- Normalized Message Envelope (SnowLuma / OneBot v11) ---
class MessageEnvelope(BaseModel):
    message_id: str
    scene: Literal["group", "private"]
    group_id: int | None = None
    sender_qq: int
    sender_nickname: str = ""
    sender_card: str = ""
    raw_text: str = ""
    reply_to_msg_id: str | None = None
    expanded_reply_text: str | None = None
    quoted_text: str | None = None
    quoted_sender_qq: int | None = None
    mentioned_qqs: list[int] = Field(default_factory=list)
    mentions_all: bool = False
    media_meta: list[dict[str, Any]] = Field(default_factory=list)
    self_qq: int = 0
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @property
    def display_name(self) -> str:
        return self.sender_card or self.sender_nickname or str(self.sender_qq)


# --- Gatekeeper Decision ---
class GatekeeperDecision(BaseModel):
    action: Literal["direct_pass", "soft_entry", "silent", "blocked_recall"]
    target: Literal["bot", "other_user", "group_open", "third_person_about_bot"]
    score: float = Field(ge=0.0, le=1.0)
    reason: str
    should_record_ambient: bool = True
    suggested_max_chars: int = 120


# --- Auth ---
class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    totp_enabled: bool = False


class PasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8)


# --- Model Control Center ---
class ModelProviderUpsert(BaseModel):
    slot: Literal["main_brain", "gatekeeper", "reflection", "embedding", "tts", "web_search"]
    role: Literal["primary", "fallback"] = "primary"
    name: str
    protocol: str = "openai_compatible"
    base_url: str
    model_name: str
    api_key: str = ""  # Empty means keep existing key on update
    extra_config: dict[str, Any] = Field(default_factory=dict)
    rate_limit_rpm: int = 60
    timeout_seconds: int = 30
    price_input_per_1m: float = 0.0
    price_output_per_1m: float = 0.0
    is_enabled: bool = True


class ModelProviderView(BaseModel):
    id: int
    slot: str
    role: str
    name: str
    protocol: str
    base_url: str
    model_name: str
    api_key_masked: str
    extra_config: dict[str, Any]
    rate_limit_rpm: int
    timeout_seconds: int
    price_input_per_1m: float
    price_output_per_1m: float
    is_enabled: bool
    last_health_status: str
    last_health_ms: int | None
    last_checked_at: datetime | None


# --- Persona & Config ---
class PersonaCreate(BaseModel):
    version_tag: str
    bot_name: str = "小呆呆"
    aliases: list[str] = Field(default_factory=lambda: ["呆呆", "小呆"])
    system_prompt: str
    few_shot_examples: list[dict[str, str]] = Field(default_factory=list)
    banned_phrases: list[str] = Field(
        default_factory=lambda: ["接住了", "记下了", "作为AI", "作为一个人工智能", "我理解你的感受", "希望这能帮到你"]
    )
    activate: bool = True


class ConfigUpsert(BaseModel):
    scope_type: Literal["global", "group", "user"]
    scope_id: str = "default"
    config_json: dict[str, Any]


# --- Memory & Identity ---
class IdentityUpdate(BaseModel):
    relationship_level: int | None = None
    public_profile: dict[str, Any] | None = None
    private_profile: dict[str, Any] | None = None


class ForgetRequest(BaseModel):
    qq_id: int
    start_time: datetime | None = None
    end_time: datetime | None = None
    wipe_profile: bool = True


class ExpressionReviewRequest(BaseModel):
    status: Literal["approved", "rejected"]
    meaning: str | None = None


# --- Alerts ---
class AlertChannelUpsert(BaseModel):
    channel_type: Literal["smtp", "webhook", "qq_admin"]
    name: str
    config: dict[str, Any]
    is_enabled: bool = True


# --- Sandbox Replay ---
class SandboxReplayRequest(BaseModel):
    group_id: int = 999999
    messages: list[dict[str, Any]]
    override_aliases: list[str] | None = None
    override_soft_entry_threshold: float | None = None
