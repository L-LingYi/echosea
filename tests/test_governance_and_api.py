"""Governance & Full FastAPI Integration Tests: Model Hub, Encryption, Fallback, Budget Fuse, EventBus, Auth, SnowLuma WS/HTTP."""
from __future__ import annotations

import asyncio

import pyotp
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.alert_service import AlertService
from app.core.event_bus import PluginEventBus
from app.core.model_hub import ModelHubService
from app.main import create_app
from app.security import decrypt_secret, encrypt_secret, mask_secret


def test_aes_gcm_encryption_and_masking():
    master = "super-secret-master-key-2026"
    plain = "sk-live-1234567890abcdef"
    enc = encrypt_secret(plain, master)
    assert enc != plain
    assert plain not in enc
    assert decrypt_secret(enc, master) == plain
    assert mask_secret(plain) == "sk-l••••cdef"


@pytest.mark.asyncio
async def test_model_hub_failover_and_budget_circuit_breaker(db_session):
    alerts = AlertService(master_key="test-master-key")
    call_log = []

    async def fake_caller(provider, api_key, messages, temperature, max_tokens):
        call_log.append((provider.role, provider.name, api_key))
        if provider.role == "primary":
            raise RuntimeError("429 Too Many Requests")
        return {
            "choices": [{"message": {"content": "备用线路回复成功"}}],
            "usage": {"prompt_tokens": 500_000, "completion_tokens": 500_000, "cached_tokens": 250_000},
        }

    hub = ModelHubService(
        master_key="test-master-key",
        alert_service=alerts,
        daily_budget_usd=1.0,
        budget_alert_ratio=0.8,
        http_caller=fake_caller,
    )
    await hub.upsert_provider(
        db_session,
        {
            "slot": "main_brain", "role": "primary", "name": "Primary-Model",
            "base_url": "https://api.primary.test/v1", "model_name": "m-primary",
            "api_key": "sk-primary-1111", "price_input_per_1m": 1.0, "price_output_per_1m": 1.0,
        },
    )
    await hub.upsert_provider(
        db_session,
        {
            "slot": "main_brain", "role": "fallback", "name": "Fallback-Model",
            "base_url": "https://api.fallback.test/v1", "model_name": "m-fallback",
            "api_key": "sk-fallback-2222", "price_input_per_1m": 1.0, "price_output_per_1m": 1.0,
        },
    )

    res = await hub.chat_complete(db_session, "main_brain", [{"role": "user", "content": "hi"}])
    assert res.used_fallback is True
    assert res.provider_name == "Fallback-Model"
    assert res.content == "备用线路回复成功"
    assert call_log == [("primary", "Primary-Model", "sk-primary-1111"), ("fallback", "Fallback-Model", "sk-fallback-2222")]
    # 500k in + 500k out at $1/1M = $1.0 >= daily_budget_usd ($1.0) -> fuse MUST activate!
    assert await hub.is_budget_fuse_active(db_session) is True


@pytest.mark.asyncio
async def test_plugin_event_bus_fault_and_timeout_isolation():
    bus = PluginEventBus(default_timeout=0.1)
    healthy_called = []

    async def broken_plugin(payload):
        raise ValueError("boom!")

    async def slow_plugin(payload):
        await asyncio.sleep(0.5)

    async def healthy_plugin(payload):
        healthy_called.append(payload["msg"])

    bus.subscribe("on_reply_sent", "broken", broken_plugin)
    bus.subscribe("on_reply_sent", "slow", slow_plugin, timeout_seconds=0.05)
    bus.subscribe("on_reply_sent", "healthy", healthy_plugin)

    result = await bus.emit("on_reply_sent", {"msg": "hello"})
    assert result["succeeded"] == ["healthy"]
    assert len(result["failed"]) == 2
    assert healthy_called == ["hello"]


def test_full_fastapi_cloud_console_and_snowluma_e2e():
    async def mock_llm(provider, api_key, messages, temperature, max_tokens):
        return {
            "choices": [{"message": {"content": "接住了！哈哈今晚吃烧烤吧，别纠结了！"}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 40, "cached_tokens": 900},
        }

    test_settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="snow-secret-token",
        snowluma_http_url="",  # Disable outbound HTTP in unit test
        bot_qq=888888,
        admin_qq=999999,
        admin_default_username="admin",
        admin_default_password="TestPassword123!",
    )
    app = create_app(settings_override=test_settings, force_memory_store=True, http_caller=mock_llm)

    with TestClient(app) as client:
        # 1. Health & WebUI index
        assert client.get("/healthz").status_code == 200
        assert "EchoSea" in client.get("/").text

        # 2. Unauthorized access rejected
        assert client.get("/api/dashboard/overview").status_code == 401

        # 3. Admin Login + JWT
        login_resp = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "TestPassword123!"},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 4. TOTP 2FA setup & enable
        setup_r = client.post("/api/auth/totp/setup", headers=headers)
        assert setup_r.status_code == 200
        secret = setup_r.json()["secret"]
        code = pyotp.TOTP(secret).now()
        enable_r = client.post("/api/auth/totp/enable", headers=headers, json={"code": code})
        assert enable_r.status_code == 200
        assert enable_r.json()["totp_enabled"] is True

        # 5. Model Control Center: upsert provider & one-click health check
        p_resp = client.put(
            "/api/providers",
            headers=headers,
            json={
                "slot": "main_brain", "role": "primary", "name": "DeepSeek-Main",
                "base_url": "https://api.deepseek.com/v1", "model_name": "deepseek-chat",
                "api_key": "sk-test-99998888", "price_input_per_1m": 0.27, "price_output_per_1m": 1.1,
            },
        )
        assert p_resp.status_code == 200
        pdata = p_resp.json()
        assert pdata["api_key_masked"] == "sk-t••••8888"
        probe = client.post(f"/api/providers/{pdata['id']}/test", headers=headers)
        assert probe.status_code == 200 and probe.json()["ok"] is True

        # 6. SnowLuma HTTP Webhook E2E: direct alias call without @ triggers reply & strips "接住了"
        ob_resp = client.post(
            "/api/onebot/event?access_token=snow-secret-token",
            json={
                "post_type": "message",
                "message_type": "group",
                "self_id": 888888,
                "group_id": 123456,
                "user_id": 55555,
                "message_id": "sl_msg_1",
                "sender": {"user_id": 55555, "nickname": "阿伟", "card": "阿伟哥"},
                "message": [{"type": "text", "data": {"text": "小呆呆，今晚吃啥？"}}],
            },
        )
        assert ob_resp.status_code == 200
        ob_data = ob_resp.json()
        assert ob_data["handled"] == "direct_pass"
        assert all("接住了" not in b for b in ob_data["bubbles"])

        # 7. SnowLuma Reverse WebSocket endpoint accepts valid token & receives heartbeat
        with client.websocket_connect("/ws/onebot/v11?access_token=snow-secret-token") as ws:
            ws.send_json({"post_type": "meta_event", "meta_event_type": "heartbeat"})
            gw = client.get("/api/onebot/status", headers=headers).json()
            assert gw["connected"] is True
            assert gw["mode"] == "reverse_ws"

        # 8. Dashboard overview reflects tokens, cache hit rate (900/1200=75%), and identities
        dash = client.get("/api/dashboard/overview?days=7", headers=headers).json()
        assert dash["tokens"]["prompt_cache_hit_rate_pct"] == 75.0
        assert dash["counts"]["qq_identities"] >= 1
        assert dash["counts"]["outbox_sent"] >= 1

        # 9. Group Chat Replay Sandbox API
        sim = client.post(
            "/api/persona/sandbox/replay",
            headers=headers,
            json={"messages": [{"sender_qq": 1001, "text": "小呆呆，帮我看看"}]},
        )
        assert sim.status_code == 200
        assert sim.json()["results"][0]["decision"]["action"] == "direct_pass"

        # 10. Alert Channel & Test Alert
        ch = client.post(
            "/api/alerts/channels",
            headers=headers,
            json={"channel_type": "qq_admin", "name": "管理员QQ", "config": {"admin_qq": 999999}},
        )
        assert ch.status_code == 200
        t_alert = client.post("/api/alerts/test", headers=headers)
        assert t_alert.status_code == 200
        assert "qq_admin:管理员QQ" in t_alert.json()["delivered_channels"]
