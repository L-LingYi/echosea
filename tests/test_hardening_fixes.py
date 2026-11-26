"""Regression tests for reliability, memory-leak, and security hardening fixes."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.core.brain_service import CompanionPipeline
from app.core.memory_service import MemoryService
from app.core.model_hub import ModelHubService
from app.core.redis_store import StateStore
from app.main import create_app
from app.security import LoginRateLimiter


def test_login_rate_limiter_prunes_empty_ips(monkeypatch):
    now = 1000.0
    monkeypatch.setattr("app.security.time.monotonic", lambda: now)

    limiter = LoginRateLimiter(max_attempts=3, window_seconds=60)
    assert limiter.is_blocked("10.0.0.1") is False
    # Checking an unseen IP must not allocate an entry in _attempts
    assert "10.0.0.1" not in limiter._attempts

    limiter.record_failure("10.0.0.1")
    limiter.record_failure("10.0.0.1")
    limiter.record_failure("10.0.0.1")
    assert limiter.is_blocked("10.0.0.1") is True

    # Advance past window -> entry is evicted completely
    now = 1061.0
    assert limiter.is_blocked("10.0.0.1") is False
    assert "10.0.0.1" not in limiter._attempts


def test_state_store_initializes_event_seq():
    store = StateStore(force_memory=True)
    assert store._event_seq == 0


async def test_group_lock_dict_evicts_unlocked_entries_at_cap():
    pipeline = CompanionPipeline(
        store=StateStore(force_memory=True),
        identity_service=MagicMock(),
        gatekeeper=MagicMock(),
        memory_service=MemoryService(),
        model_hub=MagicMock(),
        adapter=MagicMock(),
        event_bus=MagicMock(),
        sticker_service=MagicMock(),
        tts_service=MagicMock(),
        web_perception=MagicMock(),
        multimodal_service=MagicMock(),
    )
    pipeline._GROUP_DICT_MAX = 4

    for gid in range(1, 7):
        lock = pipeline._get_group_lock(gid)
        pipeline._group_generations[gid] = gid
        pipeline._group_msg_counters[gid] = gid
        assert lock is not None

    assert len(pipeline._group_locks) == 4
    assert 1 not in pipeline._group_locks
    assert 2 not in pipeline._group_locks
    assert 1 not in pipeline._group_generations
    assert 1 not in pipeline._group_msg_counters


async def test_model_hub_reuses_and_closes_http_client():
    hub = ModelHubService(master_key="test-key-32-bytes-long-for-unit-test!", alert_service=MagicMock())
    assert hub._http_client.is_closed is False
    await hub.aclose()
    assert hub._http_client.is_closed is True


async def test_production_startup_rejects_default_secrets(monkeypatch):
    prod_settings = Settings(
        environment="production",
        database_url="sqlite+aiosqlite:///:memory:",
        master_encryption_key="change-me-32-byte-master-key-for-aes-gcm!",
        jwt_secret_key="change-me-jwt-signing-secret-key-2026!",
    )
    monkeypatch.setattr("app.main.get_settings", lambda: prod_settings)
    app = create_app(settings_override=None, force_memory_store=True, enable_background_scheduler=False)

    with pytest.raises(RuntimeError, match="MASTER_ENCRYPTION_KEY"):
        async with app.router.lifespan_context(app):
            pass


async def test_forget_user_data_filters_ambient_window(db_session):
    from app.models.db import AmbientSummary

    t0 = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)
    s_in = AmbientSummary(
        group_id=100,
        window_start=t0,
        window_end=t0 + timedelta(hours=1),
        summary_text="inside",
        active_participants=[42, 99],
        topics=[],
        message_count=2,
    )
    s_out = AmbientSummary(
        group_id=100,
        window_start=t0 - timedelta(days=2),
        window_end=t0 - timedelta(days=1),
        summary_text="outside",
        active_participants=[42],
        topics=[],
        message_count=1,
    )
    s_other = AmbientSummary(
        group_id=100,
        window_start=t0,
        window_end=t0 + timedelta(hours=1),
        summary_text="other",
        active_participants=[99],
        topics=[],
        message_count=1,
    )
    db_session.add_all([s_in, s_out, s_other])
    await db_session.flush()

    svc = MemoryService()
    res = await svc.forget_user_data(
        db_session,
        qq_id=42,
        start_time=t0 - timedelta(minutes=10),
        end_time=t0 + timedelta(hours=2),
    )
    assert res["ambient_summaries_deleted"] == 1
