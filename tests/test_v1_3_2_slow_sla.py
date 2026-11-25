"""v1.3.2 Soft SLA Slow-Response Policy Test:
Verifies that slow model responses are NEVER discarded/dropped, are delivered normally,
and trigger a 'slow_response' warning alert surfaced on the Dashboard.
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_slow_reply_is_delivered_never_dropped_and_triggers_dashboard_warning():
    async def slow_mock_llm(provider, api_key, messages, temperature, max_tokens):
        await asyncio.sleep(0.08)  # Exceeds ideal_reply_ms=30ms in test
        return {
            "choices": [{"message": {"content": "虽然模型慢了点，但消息照样稳稳发出来"}}],
            "usage": {"prompt_tokens": 180, "completion_tokens": 18, "cached_tokens": 60},
        }

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-sla",
        snowluma_http_url="",
        bot_qq=888888,
        admin_default_username="admin",
        admin_default_password="SlaTestPass2026!",
        ideal_gatekeeper_ms=2000,
        ideal_reply_ms=30,  # Trigger slow-response dashboard warning at 30ms
    )
    app = create_app(settings_override=settings, force_memory_store=True, http_caller=slow_mock_llm)

    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"username": "admin", "password": "SlaTestPass2026!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        client.put(
            "/api/providers",
            headers=headers,
            json={"slot": "main_brain", "role": "primary", "name": "SlowModel", "base_url": "https://api.test/v1", "model_name": "m-slow", "api_key": "sk-1234"},
        )

        res = client.post(
            "/api/onebot/event?access_token=tok-sla",
            json={
                "post_type": "message", "message_type": "group", "self_id": 888888,
                "group_id": 486135657, "user_id": 2445955246, "message_id": "m_slow_1",
                "sender": {"nickname": "灵丶弈"},
                "message": [{"type": "text", "data": {"text": "小呆呆，慢一点也没关系"}}],
            },
        ).json()
        # MUST be delivered, NEVER dropped/aborted!
        assert res["handled"] == "direct_pass"
        assert len(res["bubbles"]) == 1
        assert "稳稳发出来" in res["bubbles"][0]

        # Dashboard & Alerts record the slow_response warning
        dash = client.get("/api/dashboard/overview?days=7", headers=headers).json()
        assert dash["tokens"]["slow_response_alerts"] >= 1
        alerts = client.get("/api/alerts/events", headers=headers).json()
        assert any(a["category"] == "slow_response" and a["severity"] == "warning" for a in alerts)
