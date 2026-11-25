"""v1.1 Diagnostics, Jev System One Gatekeeper, Consciousness Sea Graph & Benchmark Tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.snowluma_adapter import SnowLumaAdapter
from app.main import create_app


def test_v1_1_jev_gatekeeper_traces_probe_isolation_tts_receipts_and_consciousness_sea(monkeypatch):
    jev_calls = []

    async def mock_jev_systemone(provider, api_key, state_text, questions):
        jev_calls.append({"model": provider.model_name, "state": state_text, "questions": list(questions.keys())})
        # High calibrated probability to chime in on open banter
        return {
            "answers": {
                "target": {"value": "group_open", "confidence": 0.94},
                "should_chime_in": {"value": True, "probabilities": {"true": 0.89, "false": 0.11}},
            },
            "usage": {"input_tokens": 120},
        }

    async def mock_llm(provider, api_key, messages, temperature, max_tokens):
        return {
            "choices": [{"message": {"content": "哈哈确实离谱，我也想吃烧烤了"}}],
            "usage": {"prompt_tokens": 800, "completion_tokens": 25, "cached_tokens": 400},
        }

    async def mock_tts(text, opts, speed):
        return b"FAKE_MP3_AUDIO", 4.2

    async def mock_snowluma_http(self, action, params):
        assert self.http_access_token == "tok-http-v11"
        assert action == "send_group_msg"
        assert params["message"]

    monkeypatch.setattr(SnowLumaAdapter, "_send_http", mock_snowluma_http)
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-v11",
        snowluma_http_url="http://snowluma.test",
        snowluma_http_access_token="tok-http-v11",
        bot_qq=888888,
        admin_qq=999999,
        admin_default_username="admin",
        admin_default_password="V11TestPassword!",
    )
    app = create_app(
        settings_override=settings,
        force_memory_store=True,
        http_caller=mock_llm,
        tts_synthesizer=mock_tts,
        systemone_caller=mock_jev_systemone,
    )

    with TestClient(app) as client:
        # 1. Verify v1.1.0 health & mobile-responsive WebUI with Consciousness Sea
        h = client.get("/healthz").json()
        assert h["version"] == "1.1.0"
        html = client.get("/").text
        assert "意识海" in html and "mobile-bottom-nav" in html and "typesafe_systemone" in html

        token = client.post("/api/auth/login", json={"username": "admin", "password": "V11TestPassword!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 2. Configure Main Brain + Jev System One Gatekeeper
        p_main = client.put(
            "/api/providers",
            headers=headers,
            json={
                "slot": "main_brain", "role": "primary", "protocol": "openai_compatible",
                "name": "DeepSeek-V4.1", "base_url": "https://api.test/v1", "model_name": "deepseek-v4.1-flash",
                "api_key": "sk-main-1234", "price_input_per_1m": 0.2, "price_output_per_1m": 0.6,
            },
        ).json()
        p_jev = client.put(
            "/api/providers",
            headers=headers,
            json={
                "slot": "gatekeeper", "role": "primary", "protocol": "typesafe_systemone",
                "name": "TypeSafe-Jev", "base_url": "https://api.typesafe.ai", "model_name": "jev-1.14",
                "api_key": "ts-jev-9999", "price_input_per_1m": 0.042, "price_output_per_1m": 0.0,
            },
        ).json()

        # 3. Run health probes on both -> tagged source='probe', MUST NOT pollute production call count!
        assert client.post(f"/api/providers/{p_main['id']}/test", headers=headers).json()["ok"] is True
        assert client.post(f"/api/providers/{p_jev['id']}/test", headers=headers).json()["ok"] is True
        dash_before = client.get("/api/dashboard/overview?days=7", headers=headers).json()
        assert dash_before["tokens"]["total_calls"] == 0  # Probes strictly excluded!

        # 4. Send open group message (no @, no alias) -> Jev System One scores 0.89 >= 0.78 -> triggers soft_entry!
        res = client.post(
            "/api/onebot/event?access_token=tok-v11",
            json={
                "post_type": "message", "message_type": "group", "self_id": 888888,
                "group_id": 7777, "user_id": 50001, "message_id": "v11_m1",
                "sender": {"user_id": 50001, "nickname": "阿泽", "card": "阿泽哥"},
                "message": [{"type": "text", "data": {"text": "今晚烧烤摊居然排队两小时"}}],
            },
        ).json()
        assert res["handled"] == "soft_entry"
        assert res["request_id"].startswith("req_")
        assert len(jev_calls) >= 2  # 1 probe + 1 production call

        # 5. Send voice request -> verify separated TTS synthesis_status & OneBot accepted receipt
        res_v = client.post(
            "/api/onebot/event?access_token=tok-v11",
            json={
                "post_type": "message", "message_type": "group", "self_id": 888888,
                "group_id": 7777, "user_id": 50001, "message_id": "v11_m2",
                "sender": {"user_id": 50001, "nickname": "阿泽", "card": "阿泽哥"},
                "message": [{"type": "text", "data": {"text": "小呆呆，用语音回答我"}}],
            },
        ).json()
        assert res_v["handled"] == "voice_answer"
        assert res_v["tts"]["synthesis_status"] == "synthesized"
        assert res_v["tts"]["onebot_status"] == "accepted"

        # 6. Verify unified request_id traces & zero raw chat text leaked into traces
        traces = client.get(f"/api/dashboard/traces?request_id={res_v['request_id']}", headers=headers).json()
        stages = {t["stage"] for t in traces}
        assert {"inbound", "gatekeeper", "main_brain", "tts", "onebot"}.issubset(stages)
        assert "用语音回答我" not in str(traces)

        # 7. Dashboard production stats reflect real calls, P95 TTFT, and separated TTS counts
        dash = client.get("/api/dashboard/overview?days=7", headers=headers).json()
        assert dash["tokens"]["total_calls"] >= 2
        assert dash["tts"]["synthesized_count"] == 1
        assert dash["tts"]["onebot_accepted_count"] == 1
        assert dash["tts"]["onebot_acceptance_rate_pct"] == 100.0

        # 8. Consciousness Sea Memory Graph API (/api/memory/graph)
        graph = client.get("/api/memory/graph", headers=headers).json()
        node_layers = {n["layer"] for n in graph["nodes"]}
        assert "L5_self" in node_layers
        assert "L4_identity" in node_layers
        assert "group" in node_layers
        assert graph["stats"]["edge_count"] >= 2

        # 9. Labeled Gatekeeper & Voice-Intent Benchmark API
        bench = client.post(
            "/api/persona/sandbox/benchmark",
            headers=headers,
            json={
                "samples": [
                    {"sender_qq": 1001, "text": "小呆呆，帮我看看", "expected_reply": True, "expected_voice_intent": "default"},
                    {"sender_qq": 1002, "text": "帮我带杯水", "mentioned_qqs": [1003], "expected_reply": False},
                    {"sender_qq": 1004, "text": "小呆呆用语音回答我", "expected_reply": True, "expected_voice_intent": "answer_voice"},
                    {"sender_qq": 1005, "text": "小呆呆别发语音", "expected_reply": True, "expected_voice_intent": "force_text"},
                ]
            },
        ).json()
        assert bench["total_samples"] == 4
        assert bench["false_positives"] == 0
        assert bench["false_negatives"] == 0
        assert bench["voice_mismatches"] == 0
        assert bench["accuracy_pct"] == 100.0
