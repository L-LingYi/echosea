"""Application settings loaded from environment variables."""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def resolve_timezone(tz_name: str | None = None) -> tzinfo:
    """Resolve an IANA timezone name (default Asia/Shanghai), falling back to UTC+8."""
    name = (tz_name or os.environ.get("TZ") or "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=8))


def local_now(tz_name: str | None = None) -> datetime:
    """Return current timezone-aware datetime in the configured application timezone."""
    return datetime.now(resolve_timezone(tz_name))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core App
    app_name: str = "EchoSea（回声海）"
    app_version: str = "1.9.1"
    app_timezone: str = Field(default="Asia/Shanghai", validation_alias=AliasChoices("app_timezone", "tz"))
    environment: str = "production"
    log_level: str = "INFO"

    # Database & Cache
    database_url: str = "postgresql+asyncpg://companion:companion_secret@postgres:5432/companion"
    redis_url: str = "redis://redis:6379/0"

    # Security (MUST override in production .env)
    master_encryption_key: str = "change-me-32-byte-master-key-for-aes-gcm!"
    jwt_secret_key: str = "change-me-jwt-signing-secret-key-2026!"
    jwt_expire_minutes: int = 720
    admin_default_username: str = "admin"
    admin_default_password: str = "companion2026"

    # SnowLuma OneBot v11 Gateway
    snowluma_mode: str = "reverse_ws"  # reverse_ws | forward_ws | dual
    snowluma_ws_url: str = "ws://snowluma:3001/"
    snowluma_http_url: str = "http://snowluma:3000"
    snowluma_access_token: str = ""
    snowluma_http_access_token: str = Field(
        default="", validation_alias=AliasChoices("snowluma_http_access_token", "snowluma_http_token")
    )
    bot_qq: int = 0
    admin_qq: int = 0

    # Runtime Guardrails
    debounce_window_seconds: float = 2.2
    debounce_max_wait_seconds: float = 6.0
    bubble_delay_min_seconds: float = 0.8
    bubble_delay_max_seconds: float = 1.8
    enable_background_image_caption: bool = True
    max_consecutive_group_turns: int = 4
    max_attention_group_turns: int = 8        # Higher cap when user is inside active attention window (soft tightening starts at max_consecutive_group_turns)
    group_rate_limit_per_minute: int = 12
    global_rate_limit_per_minute: int = 40
    daily_budget_usd: float = 0.0
    budget_alert_ratio: float = 0.8
    soft_entry_threshold: float = 0.78
    ideal_gatekeeper_ms: int = 5000           # Soft SLA: warn on dashboard if gatekeeper exceeds this (never drops)
    ideal_reply_ms: int = 15000               # Soft SLA: warn on dashboard if total reply exceeds this
    gatekeeper_fallback_timeout_seconds: float = 18.0  # Gatekeeper safety cap -> fallback to rules + alert (never hangs 120s)
    stale_soft_entry_seconds: float = 35.0    # Drop ONLY severely stale proactive soft_entry (direct_pass is NEVER dropped)
    stale_soft_entry_min_msgs: int = 5
    attention_hot_seconds: float = 60.0       # 0-60s: hot follow-up window after strong wake-up
    attention_decay_seconds: float = 240.0    # 60-240s (4 min): linear attention decay back to normal threshold
    group_topic_warmth_seconds: float = 180.0 # 0-180s (3 min): group-level topic warmth after bot speaks so other members riffing on bot's topic don't drop
    group_topic_warmth_floor: float = 0.65    # Starting threshold right after bot speaks (decays linearly back to soft_entry_threshold)
    smart_quote_min_intervening: int = 3      # Quote only when >=3 newer group msgs pushed target up
    smart_quote_min_delay_seconds: float = 25.0  # Or when generation took >=25s
    ambient_summary_trigger_count: int = 20
    dm_bridge_window_minutes: int = 30
    plugin_timeout_seconds: float = 5.0
    searxng_url: str = "http://searxng:8080"
    default_free_search_enabled: bool = True
    webpage_fetch_enabled: bool = True
    web_search_timeout_seconds: float = 8.0

    # Industry-Standard Online Update (Privilege-Separated Host Updater Daemon)
    app_git_commit: str = ""
    update_github_repo: str = "L-LingYi/echosea"
    update_git_branch: str = "main"
    update_agent_url: str = ""            # e.g. 'http://host.docker.internal:9099'
    update_webhook_secret: str = ""       # Shared HMAC-SHA256 secret with host_updater.py

    # Graceful shutdown
    shutdown_drain_timeout_seconds: float = 15.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
