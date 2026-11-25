"""v1.3.1 Group Chat Attribution & Anti-Misaddressing Regression Tests
Reproduces the real 'City of the sky' Visa / @小呆呆 我是谁 group chat scenario:
1. Gatekeeper receives recent 5-message speaker context so peer-to-peer '你用visa...' is classified as other_user -> silent.
2. Plain-text '@小呆呆 我是谁' (even without OneBot 'at' segment) triggers direct_pass reliably.
3. Main Brain prompt anchors speaker attribution ([发言人归属锚点]) and deduplicates current message.
4. Bot's own sent reply is persisted into L1 ChatMessage history (`bot_reply`) for turn continuity.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_v1_3_1_city_of_sky_attribution_and_plain_at_alias():
    gatekeeper_states = []
    brain_prompts = []

    async def mock_jev(provider, api_key, state_text, questions):
        gatekeeper_states.append(state_text)
        # With recent context visible, Jev sees '灵丶弈' talked about USD and '如果情绪有天气' is asking 灵丶弈!
        if "灵丶弈: 美元" in state_text and "visa" in state_text:
            return {
                "answers": {
                    "target": {"value": "other_user", "confidence": 0.96},
                    "should_chime_in": {"value": False, "probabilities": {"true": 0.08, "false": 0.92}},
                },
                "usage": {"input_tokens": 90},
            }
        return {
            "answers": {
                "target": {"value": "group_open", "confidence": 0.7},
                "should_chime_in": {"value": False, "probabilities": {"true": 0.15, "false": 0.85}},
            },
            "usage": {"input_tokens": 60},
        }

    async def mock_llm(provider, api_key, messages, temperature, max_tokens):
        brain_prompts.append(messages)
        return {
            "choices": [{"message": {"content": "你是如果情绪有天气呀，刚才还吐槽灵丶弈用刀乐呢"}}],
            "usage": {"prompt_tokens": 320, "completion_tokens": 22, "cached_tokens": 120},
        }

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-sky",
        snowluma_http_url="",
        bot_qq=888888,
        admin_default_username="admin",
        admin_default_password="SkyTestPass2026!",
    )
    app = create_app(
        settings_override=settings,
        force_memory_store=True,
        http_caller=mock_llm,
        systemone_caller=mock_jev,
    )

    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"username": "admin", "password": "SkyTestPass2026!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        client.put(
            "/api/providers",
            headers=headers,
            json={"slot": "main_brain", "role": "primary", "name": "Main", "base_url": "https://api.test/v1", "model_name": "m1", "api_key": "sk-1234"},
        )
        client.put(
            "/api/providers",
            headers=headers,
            json={"slot": "gatekeeper", "role": "primary", "protocol": "typesafe_systemone", "name": "Jev", "base_url": "https://api.typesafe.ai", "model_name": "jev-1.14", "api_key": "ts-1234"},
        )

        # 1. Peer chat turns leading up to the Visa banter
        for mid, qq, name, text in [
            ("sky_1", 2445955246, "灵丶弈", "美元"),
            ("sky_2", 1283439139, "樱花色的开始", "付10刀给100刀的额度？"),
        ]:
            r = client.post(
                "/api/onebot/event?access_token=tok-sky",
                json={
                    "post_type": "message", "message_type": "group", "self_id": 888888,
                    "group_id": 486135657, "user_id": qq, "message_id": mid,
                    "sender": {"nickname": name}, "message": [{"type": "text", "data": {"text": text}}],
                },
            ).json()
            assert r["handled"] == "silent"

        # 2. '如果情绪有天气' asks '你用visa还会跟我们用QQ钱包的一起玩吗' -> Gatekeeper sees 灵丶弈 context -> silent (other_user)!
        visa_r = client.post(
            "/api/onebot/event?access_token=tok-sky",
            json={
                "post_type": "message", "message_type": "group", "self_id": 888888,
                "group_id": 486135657, "user_id": 1983133592, "message_id": "sky_visa",
                "sender": {"nickname": "如果情绪有天气"},
                "message": [{"type": "text", "data": {"text": "你用visa还会跟我们用QQ钱包的一起玩吗"}}],
            },
        ).json()
        assert visa_r["handled"] == "silent"
        assert visa_r["decision"]["target"] == "other_user"
        assert "灵丶弈: 美元" in gatekeeper_states[-1]

        # 3. Plain-text '@小呆呆 我是谁' (WITHOUT OneBot 'at' segment!) -> MUST direct_pass!
        who_r = client.post(
            "/api/onebot/event?access_token=tok-sky",
            json={
                "post_type": "message", "message_type": "group", "self_id": 888888,
                "group_id": 486135657, "user_id": 1983133592, "message_id": "sky_who",
                "sender": {"nickname": "如果情绪有天气"},
                "message": [{"type": "text", "data": {"text": "@小呆呆 我是谁"}}],
            },
        ).json()
        assert who_r["handled"] == "direct_pass"

        # Verify Main Brain prompt has explicit speaker anchor & clean QQ-tagged history without duplicating current msg
        prompt_blob = "\n".join(m["content"] for m in brain_prompts[-1])
        assert "[发言人归属锚点]" in prompt_blob
        assert "如果情绪有天气" in prompt_blob
        assert "[灵丶弈(QQ:2445955246)]: 美元" in prompt_blob

        # 4. Verify bot's own reply was recorded into L1 ChatMessage (so future turns see bot's own words)
        dash = client.get("/api/dashboard/overview?days=7", headers=headers).json()
        assert dash["gatekeeper"]["breakdown"].get("bot_reply", 0) >= 1
