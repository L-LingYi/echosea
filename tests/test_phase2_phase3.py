"""Phase 2 & Phase 3 Automated Test Suite:
- Multimodal dHash cache & 3x3 animated GIF frame-extraction grid
- Sticker auto-stealing on positive group reactions, dHash dedup, manual import & reply tag trigger
- Doubao TTS 3-intent routing (answer_voice / read_quote / force_text), text cleaning & auto-fallback
- Nightly offline reflection engine (profile interests, pending slang discovery, anniversary episodes)
- Internet hot-topic slow daily briefing (RSS XML) + fast on-demand search
- Character circadian life schedule state machine
- Standard CompanionPlugin SDK & RemoteBackupSyncPlugin failure alerting
"""
from __future__ import annotations

import io
from PIL import Image
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.identity_service import IdentityService
from app.core.life_schedule_service import LifeScheduleService
from app.core.memory_service import MemoryService
from app.core.model_hub import ModelHubService
from app.core.alert_service import AlertService
from app.core.multimodal_service import MultimodalService, compute_dhash, stitch_gif_3x3_grid
from app.core.reflection_service import ReflectionService
from app.core.sticker_service import StickerService, extract_sticker_tags
from app.core.tts_service import DoubaoTTSService, clean_text_for_tts, detect_voice_intent
from app.core.web_perception_service import WebPerceptionService, needs_fast_web_search
from app.main import create_app
from app.models.db import AlertEvent, HotTopicSource, QQIdentity
from app.models.schemas import GatekeeperDecision, MessageEnvelope


def _make_sample_png(color=(255, 0, 0)) -> bytes:
    img = Image.new("RGB", (64, 64), color)
    for x in range(32):
        img.putpixel((x, x), (0, 255, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_animated_gif(frame_count: int = 12) -> bytes:
    frames = [Image.new("RGB", (48, 48), (i * 20 % 255, 100, 200)) for i in range(frame_count)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_multimodal_3x3_gif_grid_and_dhash_vision_cache(db_session):
    gif_bytes = _make_animated_gif(12)
    grid_bytes, sampled = stitch_gif_3x3_grid(gif_bytes, tile_size=80)
    assert sampled == 9
    with Image.open(io.BytesIO(grid_bytes)) as grid_img:
        assert grid_img.size == (240, 240)

    mm = MultimodalService()
    png_bytes = _make_sample_png()
    first = await mm.lookup_or_prepare(db_session, png_bytes, source_url="https://img.test/a.png")
    assert first["cache_hit"] is False
    assert first["content_block"]["type"] == "image_url"

    await mm.save_vision_description(db_session, first["phash"], "一只在敲代码的橘猫")
    second = await mm.lookup_or_prepare(db_session, png_bytes, source_url="https://img.test/a_copy.png")
    assert second["cache_hit"] is True
    assert second["description"] == "一只在敲代码的橘猫"


@pytest.mark.asyncio
async def test_sticker_auto_stealing_dedup_and_tag_extraction(db_session, tmp_path):
    svc = StickerService(storage_dir=tmp_path / "stickers", auto_approve_threshold=3)
    png = _make_sample_png((120, 80, 200))

    # First observation -> pending
    a1 = await svc.observe_group_image(db_session, group_id=101, image_bytes=png, emotion_tags=["laugh"])
    assert a1.status == "pending"
    assert a1.appearance_count == 1

    # Duplicate image -> deduplicated via dHash
    a2 = await svc.observe_group_image(db_session, group_id=101, image_bytes=png)
    assert a2.id == a1.id
    assert a2.appearance_count == 2

    # Two positive group reactions ("哈哈", "偷了") -> automatically stolen & approved!
    await svc.observe_group_reaction_text(db_session, group_id=101, text="哈哈哈笑死")
    a3 = await svc.observe_group_reaction_text(db_session, group_id=101, text="好图偷了")
    assert a3 is not None and a3.status == "approved"

    cleaned, tags = extract_sticker_tags("太离谱了哈哈 [sticker:laugh]")
    assert cleaned == "太离谱了哈哈"
    assert tags == ["laugh"]
    picked = await svc.pick_sticker_for_tag(db_session, "laugh")
    assert picked is not None and picked.id == a1.id


@pytest.mark.asyncio
async def test_doubao_tts_three_intents_cleaning_and_fallback(db_session):
    assert detect_voice_intent("小呆呆用语音回答我") == "answer_voice"
    assert detect_voice_intent("用语音读一下", has_reply_quote=True) == "read_quote"
    assert detect_voice_intent("别发语音，用文字说") == "force_text"

    cleaned = clean_text_for_tts("**快看** https://example.com/a （偷笑）今晚吃火锅！")
    assert "https://" not in cleaned
    assert "偷笑" not in cleaned
    assert "**" not in cleaned
    assert "今晚吃火锅" in cleaned

    # Success path
    async def ok_synth(text, opts, speed):
        return b"FAKE_MP3_BYTES", 3.5

    tts_ok = DoubaoTTSService(master_key="k", synthesizer=ok_synth)
    res_ok = await tts_ok.synthesize_and_record(db_session, "group", 101, "今晚去吃烧烤", intent="answer_voice", emotion="excited")
    assert res_ok["ok"] is True
    assert res_ok["duration_seconds"] == 3.5

    # Timeout/error path -> automatic fallback to text
    async def fail_synth(text, opts, speed):
        raise TimeoutError("Doubao TTS timeout")

    tts_fail = DoubaoTTSService(master_key="k", synthesizer=fail_synth)
    res_fail = await tts_fail.synthesize_and_record(db_session, "group", 101, "超时降级测试", intent="answer_voice")
    assert res_fail["ok"] is False
    assert res_fail["fallback_text"] == "超时降级测试"


@pytest.mark.asyncio
async def test_nightly_reflection_and_circadian_schedule(db_session):
    id_svc = IdentityService()
    mem_svc = MemoryService()
    await id_svc.upsert_from_message(db_session, qq_id=30001, nickname="小王", group_id=5001)
    for i, text in enumerate([
        "我天天玩原神和健身",
        "最近群里都在刷「真香警告」",
        "哈哈哈又是「真香警告」",
        "今天是我们认识一周年纪念日！",
    ]):
        env = MessageEnvelope(message_id=f"ref_{i}", scene="group", group_id=5001, sender_qq=30001, raw_text=text, self_qq=888)
        await mem_svc.record_message(db_session, env, GatekeeperDecision(action="silent", target="group_open", score=0.2, reason="amb"))

    hub = ModelHubService(master_key="k", alert_service=AlertService(master_key="k"))
    refl = ReflectionService(model_hub=hub)
    summary = await refl.run_nightly_reflection(db_session, group_id=5001)
    assert summary["profiles_updated"] == 1
    assert summary["slang_discovered"] == 1
    assert summary["episodes_created"] == 1

    ident = await db_session.get(QQIdentity, 30001)
    assert "原神" in ident.public_profile["interests"]
    assert "健身" in ident.public_profile["interests"]

    # Circadian schedule state machine
    sched = LifeScheduleService()
    night_state = await sched.sync_current_state(db_session, local_hour=3)
    assert night_state.mood == "sleepy"
    evening_state = await sched.sync_current_state(db_session, local_hour=20)
    assert evening_state.mood == "excited"


@pytest.mark.asyncio
async def test_web_perception_dual_channel_and_full_e2e_v1():
    sample_rss = """<?xml version="1.0"?><rss><channel>
      <item><title>AI 开源社区发布新一代轻量语音模型</title></item>
      <item><title>秋季新番口碑榜单出炉</title></item>
    </channel></rss>"""

    async def fake_feed(url: str) -> str:
        return sample_rss

    async def fake_search(query: str):
        return [{"title": "新梗科普", "snippet": "这个梗源自最近的热门动画台词"}]

    captured_prompts = []

    async def fake_llm(provider, api_key, messages, temperature, max_tokens, tools=None, **_kwargs):
        captured_prompts.append(messages)
        has_tool_msg = any(m.get("role") == "tool" for m in messages)
        if tools and not has_tool_msg and any("啥梗" in str(m.get("content", "")) for m in messages if m.get("role") == "user"):
            return {
                "choices": [
                    {
                        "message": {
                            "content": "等我查一下哈",
                            "tool_calls": [
                                {
                                    "id": "call_slang_1",
                                    "type": "function",
                                    "function": {"name": "web_search", "arguments": '{"query": "这啥梗 热门动画"}'},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 420, "completion_tokens": 24, "cached_tokens": 180},
            }
        return {
            "choices": [{"message": {"content": "哈哈那个新梗我刚刷到过，太好笑了 [sticker:laugh]"}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 30, "cached_tokens": 200},
        }

    async def fake_tts(text, opts, speed):
        return b"MP3DATA", 2.8

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok",
        snowluma_http_url="",
        bot_qq=888888,
        admin_qq=999999,
        admin_default_username="admin",
        admin_default_password="FullTestPass2026!",
    )
    app = create_app(
        settings_override=settings,
        force_memory_store=True,
        http_caller=fake_llm,
        tts_synthesizer=fake_tts,
        feed_fetcher=fake_feed,
        search_caller=fake_search,
    )

    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"username": "admin", "password": "FullTestPass2026!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # Configure main_brain provider
        client.put(
            "/api/providers",
            headers=headers,
            json={"slot": "main_brain", "role": "primary", "name": "Main", "base_url": "https://api.test/v1", "model_name": "m1", "api_key": "sk-1234567890"},
        )

        # 1. Slow channel hot-topic source + refresh daily briefing
        client.post("/api/ops/hot-sources", headers=headers, json={"name": "科技热榜", "category": "tech", "feed_url": "https://rss.test/tech"})
        br = client.post("/api/ops/briefings/refresh", headers=headers)
        assert br.status_code == 200 and "新一代轻量语音模型" in br.json()["digest_text"]

        # 2. Import sticker via WebUI API
        imp = client.post(
            "/api/stickers/import",
            headers=headers,
            json={"items": [{"source_url": "https://cdn.test/laugh.gif", "emotion_tags": ["laugh"], "context_hint": "大笑"}]},
        )
        assert imp.json()["imported_count"] == 1

        # 3. Trigger message asking about new slang -> injects BOTH slow briefing & fast web search + triggers sticker!
        assert needs_fast_web_search("小呆呆，这啥梗啊？") is True
        res = client.post(
            "/api/onebot/event?access_token=tok",
            json={
                "post_type": "message", "message_type": "group", "self_id": 888888,
                "group_id": 6001, "user_id": 40001, "message_id": "e2e_1",
                "sender": {"user_id": 40001, "nickname": "阿乐"},
                "message": [{"type": "text", "data": {"text": "小呆呆，这啥梗啊？"}}],
            },
        ).json()
        assert res["handled"] == "direct_pass"
        assert res["sticker"]["tag"] == "laugh"
        prompt_blob = "\n".join(m["content"] for m in captured_prompts[-1])
        assert "新一代轻量语音模型" in prompt_blob
        assert "热门动画台词" in prompt_blob

        # 4. Trigger Doubao TTS voice answer via SnowLuma event
        res_v = client.post(
            "/api/onebot/event?access_token=tok",
            json={
                "post_type": "message", "message_type": "group", "self_id": 888888,
                "group_id": 6001, "user_id": 40001, "message_id": "e2e_2",
                "sender": {"user_id": 40001, "nickname": "阿乐"},
                "message": [{"type": "text", "data": {"text": "小呆呆，用语音回答我今晚吃啥"}}],
            },
        ).json()
        assert res_v["handled"] == "voice_answer"
        assert res_v["tts"]["ok"] is True

        # 5. Dashboard reflects TTS 100% success rate, approved stickers, and 2 built-in plugins
        dash = client.get("/api/dashboard/overview?days=7", headers=headers).json()
        assert dash["tts"]["total_attempts"] == 1
        assert dash["tts"]["success_rate_pct"] == 100.0
        assert dash["counts"]["stickers_approved"] >= 1
        assert len(dash["health"]["plugins"]) == 2

        # 6. Trigger RemoteBackupSyncPlugin failure -> automatically logs backup_failure alert!
        client.post(
            "/api/ops/backups/trigger",
            headers=headers,
            json={"filename": "fail.sql.gz", "remote_target": "aliyun_webdav", "simulated_error": "WebDAV 503"},
        )
        alerts = client.get("/api/alerts/events", headers=headers).json()
        assert any(a["category"] == "backup_failure" for a in alerts)
