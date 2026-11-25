"""Regression tests for group topic continuity, progressive attention guardrail, group warmth, and sticker hash safety."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.gatekeeper import GatekeeperService
from app.core.multimodal_service import hamming_distance
from app.core.sticker_service import StickerService
from app.main import create_app
from app.models.schemas import MessageEnvelope


@pytest.mark.asyncio
async def test_attention_window_progressive_guardrail_prevents_turn4_cutoff(state_store):
    gk = GatekeeperService(
        store=state_store,
        soft_entry_threshold=0.78,
        max_consecutive_turns=4,
        max_attention_turns=8,
        attention_hot_seconds=60.0,
        attention_decay_seconds=240.0,
    )

    # 1. Explicit @bot starts conversation and resets turns
    d0 = await gk.evaluate(
        MessageEnvelope(
            message_id="m0", scene="group", group_id=486135657, sender_qq=1983133592,
            raw_text="跑路的时候该说什么", mentioned_qqs=[2164459727], self_qq=2164459727,
        ),
        bot_name="小呆呆", aliases=["呆呆"],
    )
    assert d0.action == "direct_pass"

    # 2. Simulate 4 follow-up soft_entry turns (matching the real chat history)
    for idx in range(1, 5):
        async def _scorer(env, target):
            return {"target": "bot", "score": 0.78, "reason": f"Jev turn {idx}"}

        d = await gk.evaluate(
            MessageEnvelope(
                message_id=f"m{idx}", scene="group", group_id=486135657, sender_qq=1983133592,
                raw_text=f"追问{idx}", self_qq=2164459727,
            ),
            bot_name="小呆呆", aliases=["呆呆"], llm_scorer=_scorer,
        )
        assert d.action == "soft_entry"

    # 3. 5th follow-up ("那你再来一句我下次用") MUST NOT be hard-killed at turn=4!
    # Threshold tightened from 0.35 -> 0.43; strong follow-up passes, weak filler drops.
    async def _weak_scorer(env, target):
        return {"target": "bot", "score": 0.40, "reason": "weak filler"}

    d_weak = await gk.evaluate(
        MessageEnvelope(
            message_id="m5_weak", scene="group", group_id=486135657, sender_qq=1983133592,
            raw_text="嗯嗯", self_qq=2164459727,
        ),
        bot_name="小呆呆", aliases=["呆呆"], llm_scorer=_weak_scorer,
    )
    assert d_weak.action == "silent"
    assert "turn=4" in d_weak.reason

    async def _strong_scorer(env, target):
        return {"target": "bot", "score": 0.85, "reason": "clear follow-up request"}

    d5 = await gk.evaluate(
        MessageEnvelope(
            message_id="m5", scene="group", group_id=486135657, sender_qq=1983133592,
            raw_text="那你再来一句我下次用", self_qq=2164459727,
        ),
        bot_name="小呆呆", aliases=["呆呆"], llm_scorer=_strong_scorer,
    )
    assert d5.action == "soft_entry"
    assert "turn=4" in d5.reason

    # 4. Still enforces hard cap at max_attention_turns (8)
    for idx in range(6, 9):
        await gk.evaluate(
            MessageEnvelope(
                message_id=f"m{idx}", scene="group", group_id=486135657, sender_qq=1983133592,
                raw_text=f"继续{idx}", self_qq=2164459727,
            ),
            bot_name="小呆呆", aliases=["呆呆"], llm_scorer=_strong_scorer,
        )
    d_cap = await gk.evaluate(
        MessageEnvelope(
            message_id="m9", scene="group", group_id=486135657, sender_qq=1983133592,
            raw_text="再来一句", self_qq=2164459727,
        ),
        bot_name="小呆呆", aliases=["呆呆"], llm_scorer=_strong_scorer,
    )
    assert d_cap.action == "silent"
    assert "Air guardrail" in d_cap.reason


@pytest.mark.asyncio
async def test_group_topic_warmth_lets_other_member_riff_within_window(state_store):
    gk = GatekeeperService(
        store=state_store,
        soft_entry_threshold=0.78,
        group_topic_warmth_seconds=180.0,
        group_topic_warmth_floor=0.65,
    )
    assert gk.compute_group_warmth_threshold(0.0) == 0.65
    assert gk.compute_group_warmth_threshold(90.0) == 0.715
    assert gk.compute_group_warmth_threshold(180.0) == 0.78

    # Simulate bot replied 113s ago in the group (matching real "我说不如灰太狼" timing)
    await state_store.set_json("group_bot_active:486135657", {"ts": time.time() - 113.0}, ttl_seconds=210)

    async def _jev_075(env, target):
        return {"target": "group_open", "score": 0.75, "reason": "Jev noul=0.75"}

    # Another group member (not in personal attention window) riffs on bot's topic -> passes (~0.732 <= 0.75)
    d_riff = await gk.evaluate(
        MessageEnvelope(
            message_id="g1", scene="group", group_id=486135657, sender_qq=2023808670,
            raw_text="我说不如灰太狼", self_qq=2164459727,
        ),
        bot_name="小呆呆", aliases=["呆呆"], llm_scorer=_jev_075,
    )
    assert d_riff.action == "soft_entry"
    assert "warmth=" in d_riff.reason

    # Expired warmth window (> 180s) returns to strict 0.78 threshold -> 0.75 stays silent
    await state_store.set_json("group_bot_active:486135657", {"ts": time.time() - 200.0}, ttl_seconds=210)
    d_cold = await gk.evaluate(
        MessageEnvelope(
            message_id="g2", scene="group", group_id=486135657, sender_qq=2023808670,
            raw_text="我说不如灰太狼", self_qq=2164459727,
        ),
        bot_name="小呆呆", aliases=["呆呆"], llm_scorer=_jev_075,
    )
    assert d_cold.action == "silent"


def test_e2e_group_warmth_context_and_privacy_safe_trace_reason_code():
    jev_states: list[str] = []

    async def mock_jev(provider, api_key, state_text, questions):
        jev_states.append(state_text)
        return {
            "answers": {
                "target": {"choice": "group_open", "confidence": 0.9},
                "should_chime_in": {"noul": 0.75},
            },
            "usage": {"input_tokens": 80},
        }

    async def mock_llm(provider, api_key, messages, temperature, max_tokens):
        return {
            "choices": [{"message": {"content": "灰太狼那句我一定会回来的确实太经典了"}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 18, "cached_tokens": 100},
        }

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-warmth",
        snowluma_http_url="",
        bot_qq=2164459727,
        admin_default_username="admin",
        admin_default_password="WarmthTestPass2026!",
    )
    app = create_app(
        settings_override=settings,
        force_memory_store=True,
        http_caller=mock_llm,
        systemone_caller=mock_jev,
    )

    with TestClient(app) as client:
        token = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "WarmthTestPass2026!"},
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        client.put(
            "/api/providers",
            headers=headers,
            json={
                "slot": "main_brain", "role": "primary", "name": "Main",
                "base_url": "https://api.test/v1", "model_name": "m1", "api_key": "sk-1234",
            },
        )
        client.put(
            "/api/providers",
            headers=headers,
            json={
                "slot": "gatekeeper", "role": "primary", "protocol": "typesafe_systemone",
                "name": "Jev", "base_url": "https://api.typesafe.ai", "model_name": "jev-1.13-free", "api_key": "ts-1234",
            },
        )

        # 1. User A wakes bot -> bot replies & refreshes group_bot_active
        r1 = client.post(
            "/api/onebot/event?access_token=tok-warmth",
            json={
                "post_type": "message", "message_type": "group", "self_id": 2164459727,
                "group_id": 486135657, "user_id": 1983133592, "message_id": "u1",
                "sender": {"nickname": "如果情绪有天气"},
                "message": [{"type": "at", "data": {"qq": "2164459727"}}, {"type": "text", "data": {"text": " 再来一句我下次用"}}],
            },
        ).json()
        assert r1["handled"] == "direct_pass"

        # 2. User B chimes in with "我说不如灰太狼" (score 0.75) -> passes via group warmth & Jev sees [Recent Group Bot Topic]
        r2 = client.post(
            "/api/onebot/event?access_token=tok-warmth",
            json={
                "post_type": "message", "message_type": "group", "self_id": 2164459727,
                "group_id": 486135657, "user_id": 2023808670, "message_id": "u2",
                "sender": {"nickname": "xiuluo"},
                "message": [{"type": "text", "data": {"text": "我说不如灰太狼"}}],
            },
        ).json()
        assert r2["handled"] == "soft_entry"
        assert "[Recent Group Bot Topic]" in jev_states[-1]

        traces = client.get(f"/api/dashboard/traces?request_id={r2['request_id']}", headers=headers).json()
        gk_trace = next(t for t in traces if t["stage"] == "gatekeeper")
        assert gk_trace["meta"]["reason_code"] == "group_warmth_passed"
        assert "灰太狼" not in str(traces)


@pytest.mark.asyncio
async def test_sticker_non_hex_phash_does_not_crash_hamming_distance(db_session):
    assert hamming_distance("url_00009d84b8c0c6ee", "url_1234567890abcdef") == 64

    svc = StickerService()
    a1 = await svc.observe_group_image(
        db_session, group_id=486135657, phash="url_00009d84b8c0c6ee", source_url="https://img.test/1.png"
    )
    a2 = await svc.observe_group_image(
        db_session, group_id=486135657, phash="url_1234567890abcdef", source_url="https://img.test/2.png"
    )
    assert a1.id != a2.id
