"""v1.3.4 Tests: Official TypeSafe Jev (choice/noul) Schema & Smart Cooldown Failover / Auto-Recovery."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.alert_service import AlertService
from app.core.database import DatabaseManager
from app.core.model_hub import ModelHubService
from app.main import create_app


def test_build_systemone_url_normalization():
    assert ModelHubService._build_systemone_url("https://api.typesafe.ai") == "https://api.typesafe.ai/v1/systemone"
    assert ModelHubService._build_systemone_url("https://opencode.ai/zen/v1") == "https://opencode.ai/zen/v1/systemone"
    assert ModelHubService._build_systemone_url("https://opencode.ai/zen/v1/") == "https://opencode.ai/zen/v1/systemone"
    assert ModelHubService._build_systemone_url("https://opencode.ai/zen/v1/systemone") == "https://opencode.ai/zen/v1/systemone"


def test_parse_official_jev_choice_and_noul_response():
    raw = {
        "model": "jev-1.13-free",
        "answers": {
            "target": {
                "type": "choice",
                "choice": "other_user",
                "confidence": 0.96,
                "probabilities": {"bot": 0.01, "other_user": 0.96, "third_person_about_bot": 0.01, "group_open": 0.02},
            },
            "should_chime_in": {
                "type": "noul",
                "noul": 0.14,
            },
        },
        "usage": {"input_tokens": 310, "output_tokens": 28},
    }
    parsed = ModelHubService._parse_jev_response(raw, default_target="group_open")
    assert parsed["target"] == "other_user"
    assert parsed["score"] == 0.14
    assert "conf=0.96" in parsed["reason"]
    assert "noul=0.14" in parsed["reason"]


@pytest.mark.asyncio
async def test_jev_official_request_schema_and_quota_cooldown_auto_recovery():
    now_box = [1_760_000_000.0]
    call_log: list[dict] = []
    primary_should_429 = [True]

    async def mock_systemone(provider, api_key, state_text, questions):
        call_log.append({"provider": provider.name, "protocol": "typesafe_systemone", "questions": questions})
        # Verify official TypeSafe System One schema (choice + noul)
        assert questions["target"]["type"] == "choice"
        assert isinstance(questions["target"].get("criteria"), dict)
        assert "other_user" in questions["target"]["criteria"]
        assert questions["should_chime_in"]["type"] == "noul"

        if primary_should_429[0]:
            req = httpx.Request("POST", "https://opencode.ai/zen/v1/systemone")
            resp = httpx.Response(429, headers={"Retry-After": "120"}, text='{"error":"free quota exhausted"}', request=req)
            raise httpx.HTTPStatusError("429 Too Many Requests", request=req, response=resp)

        return {
            "model": provider.model_name,
            "answers": {
                "target": {"type": "choice", "choice": "group_open", "confidence": 0.91},
                "should_chime_in": {"type": "noul", "noul": 0.86},
            },
            "usage": {"input_tokens": 220, "output_tokens": 18},
        }

    async def mock_openai(provider, api_key, messages, temperature, max_tokens):
        call_log.append({"provider": provider.name, "protocol": "openai_compatible"})
        return {
            "choices": [{"message": {"content": '{"target":"group_open","score":0.82,"reason":"fallback deepseek ok"}'}}],
            "usage": {"prompt_tokens": 180, "completion_tokens": 22},
        }

    db_mgr = DatabaseManager()
    db_mgr.init("sqlite+aiosqlite:///:memory:")
    await db_mgr.create_all()
    hub = ModelHubService(
        master_key="test-master-key-v134",
        alert_service=AlertService(master_key="test-master-key-v134"),
        http_caller=mock_openai,
        systemone_caller=mock_systemone,
        clock=lambda: now_box[0],
    )

    async with db_mgr.session_factory() as session:
        p_primary = await hub.upsert_provider(
            session,
            {
                "slot": "gatekeeper",
                "role": "primary",
                "name": "OpenCode-Jev-Free",
                "protocol": "typesafe_systemone",
                "base_url": "https://opencode.ai/zen/v1",
                "model_name": "jev-1.13-free",
                "api_key": "oc-test-key",
                "extra_config": {"quota_cooldown_seconds": 3600},
            },
        )
        await hub.upsert_provider(
            session,
            {
                "slot": "gatekeeper",
                "role": "fallback",
                "name": "OpenCode-DeepSeek-Flash",
                "protocol": "openai_compatible",
                "base_url": "https://opencode.ai/go/v1",
                "model_name": "deepseek-v4.1-flash",
                "api_key": "oc-test-key",
            },
        )
        await session.commit()

        # Call 1: Primary hits 429 (Retry-After: 120) -> seamlessly falls back to DeepSeek & enters 120s cooldown
        res1 = await hub.evaluate_gatekeeper_slot(session, "今晚吃啥", "group_open", request_id="r1")
        assert res1 is not None and res1["score"] == 0.82
        assert [c["provider"] for c in call_log] == ["OpenCode-Jev-Free", "OpenCode-DeepSeek-Flash"]
        view1 = hub.to_safe_view(p_primary)
        assert view1["cooldown_active"] is True
        assert view1["cooldown_kind"] == "quota_exhausted"
        assert 115 <= view1["cooldown_remaining_seconds"] <= 120

        # Call 2 (60s later, still inside cooldown): Primary is SKIPPED (0ms wasted), goes straight to fallback!
        now_box[0] += 60.0
        call_log.clear()
        res2 = await hub.evaluate_gatekeeper_slot(session, "大家睡了吗", "group_open", request_id="r2")
        assert res2 is not None and res2["score"] == 0.82
        assert [c["provider"] for c in call_log] == ["OpenCode-DeepSeek-Flash"]

        # Call 3 (another 65s later -> 125s total, cooldown expired): Half-open auto-recovery probes primary!
        now_box[0] += 65.0
        primary_should_429[0] = False
        call_log.clear()
        res3 = await hub.evaluate_gatekeeper_slot(session, "哈哈笑死", "group_open", request_id="r3")
        assert res3 is not None and res3["target"] == "group_open" and res3["score"] == 0.86
        assert [c["provider"] for c in call_log] == ["OpenCode-Jev-Free"]
        view3 = hub.to_safe_view(p_primary)
        assert view3["cooldown_active"] is False
        assert view3["last_health_status"] == "healthy"


def test_probe_uses_noul_and_reset_cooldown_endpoint():
    probe_questions = []

    async def mock_systemone(provider, api_key, state_text, questions):
        probe_questions.append(questions)
        return {"answers": {"is_ping": {"type": "noul", "noul": 0.99}}, "usage": {"input_tokens": 12}}

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-v134",
        snowluma_http_url="",
        bot_qq=888888,
        admin_qq=999999,
        admin_default_username="admin",
        admin_default_password="V134TestPassword!",
    )
    app = create_app(settings_override=settings, force_memory_store=True, systemone_caller=mock_systemone)
    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"username": "admin", "password": "V134TestPassword!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        p = client.put(
            "/api/providers",
            headers=headers,
            json={
                "slot": "gatekeeper",
                "role": "primary",
                "protocol": "typesafe_systemone",
                "name": "Jev-Free",
                "base_url": "https://opencode.ai/zen/v1",
                "model_name": "jev-1.13-free",
                "api_key": "oc-key",
            },
        ).json()
        # Probe must use official `noul` type
        pr = client.post(f"/api/providers/{p['id']}/test", headers=headers).json()
        assert pr["ok"] is True
        assert probe_questions[0]["is_ping"]["type"] == "noul"

        # Reset cooldown API responds cleanly
        rr = client.post(f"/api/providers/{p['id']}/reset-cooldown", headers=headers).json()
        assert rr["ok"] is True
        assert rr["provider"]["cooldown_active"] is False


def test_token_only_dashboard_without_money_ui():
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-v135",
        snowluma_http_url="",
        bot_qq=888888,
        admin_qq=999999,
        admin_default_username="admin",
        admin_default_password="V135TestPassword!",
    )
    assert settings.daily_budget_usd == 0.0
    app = create_app(settings_override=settings, force_memory_store=True)
    with TestClient(app) as client:
        html = client.get("/").text
        assert "今日 Token 消耗" in html
        assert "今日费用 / 预算" not in html
        assert "预估费用 (USD)" not in html
        token = client.post("/api/auth/login", json={"username": "admin", "password": "V135TestPassword!"}).json()["access_token"]
        dash = client.get("/api/dashboard/overview?days=7", headers={"Authorization": f"Bearer {token}"}).json()
        assert "today_total_tokens" in dash["tokens"]
        assert "total_tokens" in dash["tokens"]
