"""v1.3.0 Production Optimization Regression Tests:
1. Smart Buried-Only Quoting (no quote on fresh latest message; quotes when >=3 intervening msgs or >=25s delay).
2. Empty message (no text + no media) ignored instead of generating awkward reply.
3. AI context-aware sticker auto-tagging (heuristic & API endpoint).
4. Polished WebUI renders visual progress bars, modal provider editor, and sticker card gallery.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.brain_service import CompanionPipeline
from app.core.event_bus import PluginEventBus
from app.core.gatekeeper import GatekeeperService
from app.core.identity_service import IdentityService
from app.core.memory_service import MemoryService
from app.core.model_hub import ModelHubService
from app.core.alert_service import AlertService
from app.core.snowluma_adapter import SnowLumaAdapter
from app.core.sticker_service import StickerService
from app.main import create_app
from app.models.schemas import GatekeeperDecision, MessageEnvelope


@pytest.mark.asyncio
async def test_smart_buried_only_quoting_thresholds(db_session, state_store):
    id_svc = IdentityService()
    mem_svc = MemoryService()
    await id_svc.upsert_from_message(db_session, qq_id=1001, nickname="阿明", group_id=8801)

    target_env = MessageEnvelope(
        message_id="m_target", scene="group", group_id=8801, sender_qq=1001, raw_text="小呆呆在吗", self_qq=888
    )
    await mem_svc.record_message(
        db_session, target_env, GatekeeperDecision(action="direct_pass", target="bot", score=1.0, reason="direct")
    )

    pipeline = CompanionPipeline(
        store=state_store,
        identity_service=id_svc,
        gatekeeper=GatekeeperService(store=state_store),
        memory_service=mem_svc,
        model_hub=ModelHubService(master_key="k", alert_service=AlertService(master_key="k")),
        adapter=SnowLumaAdapter(bot_qq=888),
        event_bus=PluginEventBus(),
        smart_quote_min_intervening=3,
        smart_quote_min_delay_seconds=25.0,
    )

    # 1. Target is still at bottom of screen (0 intervening msgs, 2s elapsed) -> DO NOT quote!
    assert await pipeline.should_quote_target(db_session, 8801, "m_target", elapsed_seconds=2.0) is False

    # 2. Slow reply (>= 25s) -> DO quote!
    assert await pipeline.should_quote_target(db_session, 8801, "m_target", elapsed_seconds=28.0) is True

    # 3. Push 3 newer messages into the group -> buried! -> DO quote even if fast!
    for i in range(3):
        await mem_svc.record_message(
            db_session,
            MessageEnvelope(message_id=f"m_new_{i}", scene="group", group_id=8801, sender_qq=1001, raw_text=f"刷屏{i}", self_qq=888),
            GatekeeperDecision(action="silent", target="group_open", score=0.1, reason="amb"),
        )
    assert await pipeline.should_quote_target(db_session, 8801, "m_target", elapsed_seconds=1.5) is True


@pytest.mark.asyncio
async def test_sticker_context_aware_auto_tagging(db_session):
    svc = StickerService()
    asset = await svc.observe_group_image(db_session, group_id=486135657, phash="abcd1234abcd1234", source_url="https://img.test/oof.gif")
    updated = await svc.auto_tag_with_context(
        db_session,
        asset=asset,
        surrounding_messages=[
            "樱花色的开始: 我已经欠费了",
            "樱花色的开始: 接入的模型直接把DS取代了，太离谱",
        ],
    )
    assert "崩溃无语" in updated.emotion_tags
    assert "踩坑" in updated.context_hint or "离谱" in updated.context_hint


def test_v1_3_e2e_no_unnecessary_quote_empty_guard_and_polished_ui():
    async def mock_llm(provider, api_key, messages, temperature, max_tokens):
        return {
            "choices": [{"message": {"content": "在的呢，刚刷完群"}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 12, "cached_tokens": 100},
        }

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-v13",
        snowluma_http_url="",
        bot_qq=888888,
        admin_default_username="admin",
        admin_default_password="V13TestPass!",
    )
    app = create_app(settings_override=settings, force_memory_store=True, http_caller=mock_llm)

    with TestClient(app) as client:
        # 1. Verify polished WebUI markers
        html = client.get("/").text
        stickers_js = client.get("/static/js/stickers.js").text
        dashboard_js = client.get("/static/js/dashboard.js").text
        assert "providerModal" in html
        assert "stickerGrid" in html
        assert "autoTagSticker(" in stickers_js
        assert "progress-fill" in dashboard_js

        token = client.post("/api/auth/login", json={"username": "admin", "password": "V13TestPass!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        client.put(
            "/api/providers",
            headers=headers,
            json={"slot": "main_brain", "role": "primary", "name": "Main", "base_url": "https://api.test/v1", "model_name": "m1", "api_key": "sk-1234"},
        )

        # 2. Empty message with no text and no media -> ignored cleanly!
        empty_res = client.post(
            "/api/onebot/event?access_token=tok-v13",
            json={
                "post_type": "message", "message_type": "group", "self_id": 888888,
                "group_id": 486135657, "user_id": 12345, "message_id": "m_empty",
                "sender": {"nickname": "测试"}, "message": [{"type": "text", "data": {"text": "   "}}],
            },
        ).json()
        assert empty_res["handled"] == "empty_message_ignored"

        # 3. Fresh group message (0 messages after it) -> replied WITHOUT unnecessary quote!
        fresh_res = client.post(
            "/api/onebot/event?access_token=tok-v13",
            json={
                "post_type": "message", "message_type": "group", "self_id": 888888,
                "group_id": 486135657, "user_id": 12345, "message_id": "m_fresh",
                "sender": {"nickname": "测试"}, "message": [{"type": "text", "data": {"text": "小呆呆在吗"}}],
            },
        ).json()
        assert fresh_res["handled"] == "direct_pass"
        assert app.state.adapter.sent_log[-1]["reply_to_msg_id"] is None

        # 4. Sticker auto-tag API endpoint
        client.post(
            "/api/stickers/import",
            headers=headers,
            json={"items": [{"source_url": "https://img.test/lol.gif", "emotion_tags": ["fun"], "context_hint": "哈哈哈笑死"}]},
        )
        stickers = client.get("/api/stickers", headers=headers).json()
        tag_res = client.post(f"/api/stickers/{stickers[0]['id']}/auto-tag", headers=headers, json={}).json()
        assert tag_res["ok"] is True
        assert "大笑" in tag_res["emotion_tags"]
