"""v1.1.1 Ops Hardening Regression Tests:
1. SQL migration parser never skips statements preceded by '--' comments.
2. Reply quote expansion enforces same-scene/same-group & is_recalled=False boundaries.
3. PUT/PATCH /api/providers preserves extra_config (Volcengine TTS appid/cluster) when omitted.
4. Voice send failure automatically falls back to text delivery; dashboard keeps synthesis rate + separate accept rate.
5. Unified WebUI '记忆意识海' (no duplicate memory tab) + XSS esc() helper.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.database import split_sql_statements_safe
from app.core.identity_service import IdentityService
from app.core.memory_service import MemoryService
from app.core.snowluma_adapter import SnowLumaAdapter
from app.main import create_app
from app.models.schemas import GatekeeperDecision, MessageEnvelope


def test_sql_migration_never_skips_commented_statements():
    script = """
    -- 1. Leading comment before ALTER TABLE
    ALTER TABLE memory_episodes ADD COLUMN IF NOT EXISTS embedding_json JSONB;

    -- 2. Another comment
    -- Multi-line comment
    CREATE TABLE IF NOT EXISTS pipeline_traces (id INT);
    """
    stmts = split_sql_statements_safe(script)
    assert len(stmts) == 2
    assert stmts[0].startswith("ALTER TABLE memory_episodes")
    assert stmts[1].startswith("CREATE TABLE IF NOT EXISTS pipeline_traces")


@pytest.mark.asyncio
async def test_reply_quote_enforces_session_scope_and_recall_filter(db_session):
    adapter = SnowLumaAdapter(bot_qq=888888)
    id_svc = IdentityService()
    mem_svc = MemoryService()

    await id_svc.upsert_from_message(db_session, qq_id=10001, nickname="张三", group_id=7001, group_card="三哥")
    # Seed a normal message in group 7001 and a recalled message in group 7001
    await mem_svc.record_message(
        db_session,
        MessageEnvelope(message_id="m_valid", scene="group", group_id=7001, sender_qq=10001, sender_card="三哥", raw_text="群7001原话", self_qq=888888),
        GatekeeperDecision(action="silent", target="group_open", score=0.2, reason="t"),
    )
    await mem_svc.record_message(
        db_session,
        MessageEnvelope(message_id="m_recalled", scene="group", group_id=7001, sender_qq=10001, sender_card="三哥", raw_text="已撤回内容", self_qq=888888),
        GatekeeperDecision(action="blocked_recall", target="group_open", score=0.0, reason="recalled"),
    )

    # 1. Cross-group quote (group 7002 quoting group 7001's msg) -> MUST be blocked (None)
    cross_group = await adapter.normalize_event(
        {
            "post_type": "message", "message_type": "group", "self_id": 888888, "group_id": 7002,
            "user_id": 10002, "message_id": "m_x1", "sender": {"nickname": "李四"},
            "message": [{"type": "reply", "data": {"id": "m_valid"}}, {"type": "text", "data": {"text": "跨群偷看"}}],
        },
        session=db_session,
    )
    assert cross_group is not None and cross_group.expanded_reply_text is None

    # 2. Quoting a recalled message in the same group -> MUST be blocked (None)
    recalled_quote = await adapter.normalize_event(
        {
            "post_type": "message", "message_type": "group", "self_id": 888888, "group_id": 7001,
            "user_id": 10002, "message_id": "m_x2", "sender": {"nickname": "李四"},
            "message": [{"type": "reply", "data": {"id": "m_recalled"}}, {"type": "text", "data": {"text": "引用撤回"}}],
        },
        session=db_session,
    )
    assert recalled_quote is not None and recalled_quote.expanded_reply_text is None

    # 3. Same-group non-recalled quote -> succeeds!
    valid_quote = await adapter.normalize_event(
        {
            "post_type": "message", "message_type": "group", "self_id": 888888, "group_id": 7001,
            "user_id": 10002, "message_id": "m_x3", "sender": {"nickname": "李四"},
            "message": [{"type": "reply", "data": {"id": "m_valid"}}, {"type": "text", "data": {"text": "正常引用"}}],
        },
        session=db_session,
    )
    assert valid_quote is not None and valid_quote.expanded_reply_text == "三哥: 群7001原话"


def test_provider_extra_config_preserved_and_unified_memory_sea_ui():
    async def mock_llm(provider, api_key, messages, temperature, max_tokens):
        return {"choices": [{"message": {"content": "测试回复"}}], "usage": {"prompt_tokens": 100, "completion_tokens": 10}}

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok",
        snowluma_http_url="",
        bot_qq=888888,
        admin_default_username="admin",
        admin_default_password="HardeningPass2026!",
    )
    app = create_app(settings_override=settings, force_memory_store=True, http_caller=mock_llm)

    with TestClient(app) as client:
        # Verify unified Memory Sea UI (no duplicate tab-memory) & XSS esc() helper
        html = client.get("/").text
        assert "记忆意识海" in html
        assert 'id="tab-memory"' not in html
        assert "function esc(" in html
        assert "editProvider(" in html

        token = client.post("/api/auth/login", json={"username": "admin", "password": "HardeningPass2026!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 1. Create Volcengine TTS provider with extra_config (appid + cluster)
        r1 = client.put(
            "/api/providers",
            headers=headers,
            json={
                "slot": "tts", "role": "primary", "name": "Volcengine-V3",
                "base_url": "https://openspeech.bytedance.com/api/v3/tts", "model_name": "daidai-2",
                "api_key": "volc-secret-1234",
                "extra_config": {"appid": "998877", "cluster": "volcano_tts", "api_version": "v3"},
            },
        ).json()
        assert r1["extra_config"]["appid"] == "998877"

        # 2. Update via PUT WITHOUT sending extra_config -> MUST preserve appid/cluster!
        r2 = client.put(
            "/api/providers",
            headers=headers,
            json={
                "slot": "tts", "role": "primary", "name": "Volcengine-V3-Updated",
                "base_url": "https://openspeech.bytedance.com/api/v3/tts", "model_name": "daidai-2",
                "api_key": "",
            },
        ).json()
        assert r2["name"] == "Volcengine-V3-Updated"
        assert r2["extra_config"]["appid"] == "998877"
        assert r2["api_key_masked"] == "volc••••1234"

        # 3. Partial update via PATCH /api/providers/{id}
        r3 = client.patch(
            f"/api/providers/{r1['id']}",
            headers=headers,
            json={"timeout_seconds": 45},
        ).json()
        assert r3["timeout_seconds"] == 45
        assert r3["extra_config"]["cluster"] == "volcano_tts"
