"""Application settings loaded from environment variables."""
from __future__ import annotations

import secrets
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core App
    app_name: str = "Companion System"
    app_version: str = "1.1.0"
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
    bot_qq: int = 0
    admin_qq: int = 0

    # Runtime Guardrails
    debounce_window_seconds: float = 2.5
    max_consecutive_group_turns: int = 4
    group_rate_limit_per_minute: int = 12
    global_rate_limit_per_minute: int = 40
    daily_budget_usd: float = 5.0
    budget_alert_ratio: float = 0.8
    soft_entry_threshold: float = 0.78
    ambient_summary_trigger_count: int = 20
    dm_bridge_window_minutes: int = 30
    plugin_timeout_seconds: float = 5.0

    # Graceful shutdown
    shutdown_drain_timeout_seconds: float = 15.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
