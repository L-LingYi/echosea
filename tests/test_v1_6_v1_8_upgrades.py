"""Comprehensive regression tests for EchoSea v1.6.0, v1.7.0, and v1.8.0 upgrades."""
from __future__ import annotations

import asyncio
import gzip
import importlib.util
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.api.auth import _extract_client_ip, _is_https_request, _is_trusted_proxy
from app.api.onebot import _verify_onebot_token
from app.config import Settings
from app.core.alert_service import AlertService
from app.core.backup_service import BackupService
from app.core.brain_service import CompanionPipeline, WEB_SEARCH_TOOLS
from app.core.database import DatabaseManager
from app.core.event_bus import PluginEventBus
from app.core.gatekeeper import GatekeeperService
from app.core.identity_service import IdentityService
from app.core.life_schedule_service import LifeScheduleService
from app.core.memory_service import MemoryService
from app.core.model_hub import ModelHubService
from app.core.multimodal_service import MultimodalService, deterministic_url_hash
from app.core.redis_store import MessageDebouncer, StateStore
from app.core.reflection_service import ReflectionService
from app.core.scheduler import CoreScheduler
from app.core.snowluma_adapter import SnowLumaAdapter
from app.core.sticker_service import StickerService
from app.core.web_perception_service import WebPerceptionService
from app.main import create_app
from app.models.db import ImageVisionCache, MemoryEpisode, PersonaVersion, PipelineTrace, QQIdentity, UsageMetric
from app.models.schemas import GatekeeperDecision, MessageEnvelope
from app.plugins.sdk import AutoStickerReactionPlugin


def _make_png_bytes(color_left: tuple[int, int, int], color_right: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (32, 32), color_left)
    for x in range(16, 32):
        for y in range(32):
            img.putpixel((x, y), color_right)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_v160_event_bus_background_drain_and_bounded_logs():
    bus = PluginEventBus(default_timeout=2.0, max_error_log=5)
    finished: list[str] = []

    async def slow_ok(payload):
        await asyncio.sleep(0.05)
        finished.append(payload["id"])

    async def fail_plugin(_payload):
        raise RuntimeError("boom")

    bus.subscribe("ev", "ok_p", slow_ok)
    bus.subscribe("ev", "err_p", fail_plugin)
    for i in range(6):
        await bus.emit("ev", {"id": f"e{i}"})
    assert len(bus.error_log) == 5

    tasks = bus.emit_background("ev", {"id": "bg1"}, max_concurrency=1)
    assert len(tasks) == 2
    await bus.drain_background(timeout=2.0)
    assert "bg1" in finished


@pytest.mark.asyncio
async def test_v160_sticker_deterministic_dhash_and_manual_sha256(db_session):
    png_bytes = _make_png_bytes((20, 20, 20), (240, 240, 240))
    svc = StickerService()
    created = await svc.import_manual(
        db_session,
        [{"source_url": "https://cdn.example.com/a.png", "emotion_tags": ["笑死"]}],
    )
    assert created[0].phash == deterministic_url_hash("https://cdn.example.com/a.png", prefix="manual")

    bus = PluginEventBus()
    mm = MultimodalService(image_fetcher=lambda _url: asyncio.sleep(0, result=png_bytes))
    runtime = SimpleNamespace(
        db_manager=SimpleNamespace(session_factory=lambda: _AsyncCtx(db_session)),
        sticker_service=svc,
        model_hub=None,
        multimodal_service=mm,
    )
    AutoStickerReactionPlugin().register(bus, runtime)
    res = await bus.emit(
        "on_message_evaluated",
        {
            "envelope": {
                "scene": "group",
                "group_id": 777,
                "raw_text": "哈哈哈哈",
                "media_meta": [{"type": "image", "url": "https://img.example.com/pic.png"}],
            }
        },
    )
    assert res["failed"] == []


class _AsyncCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_v160_gatekeeper_no_threshold_mutation_and_prompt_cache_prefix(db_session):
    async def mock_llm(_p, _k, _msgs, _t, _m, **_kw):
        return {"choices": [{"message": {"content": "确实离谱哈哈"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    store = StateStore(force_memory=True)
    gk = GatekeeperService(store=store, soft_entry_threshold=0.78)
    id_svc = IdentityService()
    mem = MemoryService()
    hub = ModelHubService("k-v16", AlertService("k-v16"), http_caller=mock_llm)
    await hub.upsert_provider(
        db_session,
        {
            "slot": "main_brain",
            "role": "primary",
            "name": "Brain-v16",
            "protocol": "openai_compatible",
            "base_url": "https://api.example.com/v1",
            "model_name": "m1",
            "api_key": "k",
        },
    )
    adapter = SnowLumaAdapter()
    bus = PluginEventBus()
    pipe = CompanionPipeline(store, id_svc, gk, mem, hub, adapter, bus)

    from app.models.db import SystemConfig

    db_session.add(SystemConfig(scope_type="group", scope_id="9001", config_json={"soft_entry_threshold": 0.42}))
    await db_session.commit()

    env = MessageEnvelope(
        message_id="m_gk_iso",
        scene="group",
        group_id=9001,
        sender_qq=111222,
        sender_nickname="群友A",
        raw_text="哈哈有点离谱，真的吗？",
        self_qq=888888,
    )
    await pipe.process_envelope(db_session, env)
    # Global GatekeeperService instance must NOT be mutated by group 9001 override
    assert gk.soft_entry_threshold == pytest.approx(0.78)

    # Prompt cache prefix: static persona + search policy stays identical when L5 mood/energy changes
    persona = await pipe.get_active_persona(db_session)
    dec = GatekeeperDecision(action="direct_pass", target="bot", score=1.0, reason="direct")
    ctx1 = {
        "l5_self_state": {"energy_level": 90, "mood": "happy", "current_activity": "摸鱼", "diary_entry": ""},
        "l4_acquaintance": {"qq_id": 1},
        "l4_group_slang": [],
        "l2_ambient_summaries": [],
        "l1_group_to_dm_bridge": [],
        "l3_episodes": [],
        "l1_working": [],
    }
    ctx2 = {**ctx1, "l5_self_state": {"energy_level": 20, "mood": "sleepy", "current_activity": "发呆", "diary_entry": ""}}
    msgs1 = pipe.build_prompt_messages(persona, ctx1, env, dec, briefing_text="今日热搜", enable_search_tools=True)
    msgs2 = pipe.build_prompt_messages(persona, ctx2, env, dec, briefing_text="今日热搜", enable_search_tools=True)
    assert msgs1[0]["content"] == msgs2[0]["content"]
    assert "[联网工具使用准则]" in msgs1[0]["content"]
    assert "[今日刷网背景常识]" in msgs1[-3]["content"]
    assert "精力:90" in msgs1[-2]["content"]
    assert "精力:20" in msgs2[-2]["content"]


@pytest.mark.asyncio
async def test_v160_gatekeeper_completion_tokens_and_cost(db_session):
    async def mock_sysone(provider, api_key, state_text, questions):
        return {
            "answers": {
                "target": {"type": "choice", "choice": "bot", "confidence": 0.96},
                "should_chime_in": {"type": "noul", "noul": 0.88},
            },
            "usage": {"input_tokens": 1000, "output_tokens": 500},
        }

    hub = ModelHubService("k-tok", AlertService("k-tok"), systemone_caller=mock_sysone)
    await hub.upsert_provider(
        db_session,
        {
            "slot": "gatekeeper",
            "role": "primary",
            "name": "Jev-Paid",
            "protocol": "typesafe_systemone",
            "base_url": "https://api.example.com",
            "model_name": "jev-1.14",
            "api_key": "k1",
            "price_input_per_1m": 1.0,
            "price_output_per_1m": 4.0,
        },
    )
    await db_session.commit()
    res = await hub.evaluate_gatekeeper_slot(db_session, "hello", "group_open", request_id="req_gk_tok")
    assert res["target"] == "bot"
    from sqlalchemy import select

    metric = (await db_session.execute(select(UsageMetric).where(UsageMetric.request_id == "req_gk_tok"))).scalar_one()
    assert metric.prompt_tokens == 1000
    assert metric.completion_tokens == 500
    # 1000/1e6 * 1.0 + 500/1e6 * 4.0 = 0.003
    assert metric.estimated_cost_usd == pytest.approx(0.003)


def test_v160_auth_security_proxy_cookie_and_token_revocation():
    assert _is_trusted_proxy("127.0.0.1") is True
    assert _is_trusted_proxy("172.18.0.5") is True
    assert _is_trusted_proxy("8.8.8.8") is False

    fake_public_req = SimpleNamespace(
        client=SimpleNamespace(host="198.51.100.10"),
        headers={"x-real-ip": "1.2.3.4", "x-forwarded-proto": "https"},
        url=SimpleNamespace(scheme="http"),
    )
    assert _extract_client_ip(fake_public_req) == "198.51.100.10"
    assert _is_https_request(fake_public_req) is False

    fake_proxy_req = SimpleNamespace(
        client=SimpleNamespace(host="172.18.0.2"),
        headers={"x-real-ip": "203.0.113.5", "x-forwarded-proto": "https"},
        url=SimpleNamespace(scheme="http"),
    )
    assert _extract_client_ip(fake_proxy_req) == "203.0.113.5"
    assert _is_https_request(fake_proxy_req) is True

    assert _verify_onebot_token("secret-abc", "Bearer secret-abc", None) is True
    assert _verify_onebot_token("secret-abc", "Bearer wrong", None) is False

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-sec",
        snowluma_http_url="",
        admin_default_username="admin",
        admin_default_password="OldPassword123!",
    )
    app = create_app(settings_override=settings, force_memory_store=True)
    with TestClient(app, base_url="https://testserver") as client:
        # 5 failed attempts from testclient -> 6th blocked with 429 (testclient cannot spoof X-Real-IP!)
        for _ in range(5):
            r_bad = client.post("/api/auth/login", headers={"X-Real-IP": "9.9.9.9"}, json={"username": "admin", "password": "wrong"})
            assert r_bad.status_code == 401
        r_429 = client.post("/api/auth/login", headers={"X-Real-IP": "9.9.9.10"}, json={"username": "admin", "password": "wrong"})
        assert r_429.status_code == 429

        # Reset testclient bucket in state store to simulate valid login & test HTTPS Secure cookie + revocation
        import asyncio as _aio
        _aio.run(app.state.state_store.reset_login_failures("testclient"))
        r_ok = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "OldPassword123!"},
        )
        assert r_ok.status_code == 200
        set_cookie = r_ok.headers.get("set-cookie", "").lower()
        assert "httponly" in set_cookie and "secure" in set_cookie
        tok1 = r_ok.json()["access_token"]
        assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok1}"}).status_code == 200

        # Change password -> increments token_version -> tok1 immediately revoked!
        cp = client.post(
            "/api/auth/password",
            headers={"Authorization": f"Bearer {tok1}"},
            json={"old_password": "OldPassword123!", "new_password": "NewPassword456!"},
        )
        assert cp.status_code == 200
        assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok1}"}).status_code == 401

        # Login with new password -> logout revokes new token immediately!
        r_ok2 = client.post("/api/auth/login", json={"username": "admin", "password": "NewPassword456!"})
        tok2 = r_ok2.json()["access_token"]
        assert client.post("/api/auth/logout", headers={"Authorization": f"Bearer {tok2}"}).status_code == 200
        assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok2}"}).status_code == 401


@pytest.mark.asyncio
async def test_v160_core_scheduler_idempotent_daily_runs(db_session, tmp_path):
    calls: list[str] = []

    async def fake_feed(_url: str) -> str:
        calls.append("feed")
        return "<rss><channel><item><title>AI 新动态</title></item></channel></rss>"

    async def fake_llm(_provider, _key, _msgs, _t, _m, **_kw):
        calls.append("llm")
        return {"choices": [{"message": {"content": '{"profile_clues":[],"slang_candidates":[],"episodes":[]}'}}]}

    hub = ModelHubService("k-sched", AlertService("k-sched"), http_caller=fake_llm)
    life = LifeScheduleService(timezone_name="Asia/Shanghai")
    wp = WebPerceptionService("k-sched", feed_fetcher=fake_feed, timezone_name="Asia/Shanghai")
    refl = ReflectionService(hub, timezone_name="Asia/Shanghai")
    bk = BackupService("k-sched", local_backup_dir=str(tmp_path), timezone_name="Asia/Shanghai")

    scheduler = CoreScheduler(life, wp, refl, bk, timezone_name="Asia/Shanghai")

    # Simulated 09:00 Asia/Shanghai (01:00 UTC) -> triggers schedule sync, daily briefing, nightly reflection
    t_morning = datetime(2026, 11, 26, 1, 0, tzinfo=timezone.utc)
    res1 = await scheduler.run_tick(db_session, now=t_morning)
    assert "life_schedule" in res1["executed"]
    assert "daily_briefing" in res1["executed"]
    assert "nightly_reflection" in res1["executed"]

    # Immediate second tick 1 minute later -> all daily & 15-min jobs skipped idempotently
    res2 = await scheduler.run_tick(db_session, now=t_morning + timedelta(minutes=1))
    assert res2["executed"] == []

    # 20 minutes later -> only 15-min life_schedule runs again
    res3 = await scheduler.run_tick(db_session, now=t_morning + timedelta(minutes=20))
    assert res3["executed"] == ["life_schedule"]


def test_v160_host_updater_broken_pipe_and_check_cache(monkeypatch):
    spec = importlib.util.spec_from_file_location("host_updater", Path("ops/updater/host_updater.py"))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    dummy = mod.Handler.__new__(mod.Handler)
    dummy.send_response = lambda _c: None
    dummy.send_header = lambda _k, _v: None
    dummy.end_headers = lambda: None
    dummy.wfile = SimpleNamespace(write=lambda _b: (_ for _ in ()).throw(BrokenPipeError("client gone")))
    # Must swallow BrokenPipeError without raising
    dummy.reply(200, {"ok": True})

    mod.invalidate_check_cache()
    assert mod._CHECK_CACHE["data"] is None


@pytest.mark.asyncio
async def test_v170_debouncer_recall_drop_group_lock_and_multimodal_cache(db_session):
    debouncer = MessageDebouncer(window_seconds=0.08)
    batches: list[list[dict]] = []

    async def on_batch(items):
        batches.append(items)

    await debouncer.push("k1", {"message_id": "m1", "raw_text": "第一句"}, on_batch)
    await debouncer.push("k1", {"message_id": "m2", "raw_text": "第二句"}, on_batch)
    removed = debouncer.remove_recalled_message("m1")
    assert removed == 1
    await asyncio.sleep(0.15)
    assert len(batches) == 1 and len(batches[0]) == 1 and batches[0][0]["message_id"] == "m2"

    # End-to-end multimodal pipeline integration + per-group lock serialization
    png_bytes = _make_png_bytes((10, 80, 120), (220, 180, 50))
    seen_payloads: list[dict] = []

    async def fake_llm(provider, key, messages, temp, max_tokens, **_kw):
        seen_payloads.append({"messages": messages})
        return {"choices": [{"message": {"content": "这图配色挺骚气啊"}}], "usage": {"prompt_tokens": 50, "completion_tokens": 12}}

    store = StateStore(force_memory=True)
    hub = ModelHubService("k-mm", AlertService("k-mm"), http_caller=fake_llm)
    await hub.upsert_provider(
        db_session,
        {
            "slot": "main_brain",
            "role": "primary",
            "name": "MM-Brain",
            "protocol": "openai_compatible",
            "base_url": "https://api.example.com/v1",
            "model_name": "gpt-4o",
            "api_key": "k-mm",
        },
    )
    mm = MultimodalService(image_fetcher=lambda _url: asyncio.sleep(0, result=png_bytes))
    pipe = CompanionPipeline(
        store=store,
        identity_service=IdentityService(),
        gatekeeper=GatekeeperService(store=store),
        memory_service=MemoryService(),
        model_hub=hub,
        adapter=SnowLumaAdapter(),
        event_bus=PluginEventBus(),
        multimodal_service=mm,
    )

    env1 = MessageEnvelope(
        message_id="mm1",
        scene="group",
        group_id=505,
        sender_qq=1001,
        sender_nickname="阿图",
        raw_text="小呆呆看看这张图",
        mentioned_qqs=[888888],
        self_qq=888888,
        media_meta=[{"type": "image", "url": "https://img.example.com/test1.png"}],
    )
    r1 = await pipe.process_envelope(db_session, env1)
    assert r1["handled"] == "direct_pass"
    # First turn sends multimodal content block list
    assert isinstance(seen_payloads[0]["messages"][-1]["content"], list)
    assert seen_payloads[0]["messages"][-1]["content"][1]["type"] == "image_url"

    # Second turn with same image hits ImageVisionCache -> zero extra vision block!
    env2 = MessageEnvelope(
        message_id="mm2",
        scene="group",
        group_id=505,
        sender_qq=1002,
        sender_nickname="阿二",
        raw_text="小呆呆这张图呢",
        mentioned_qqs=[888888],
        self_qq=888888,
        media_meta=[{"type": "image", "url": "https://img.example.com/test2.png"}],
    )
    r2 = await pipe.process_envelope(db_session, env2)
    assert r2["handled"] == "direct_pass"
    assert isinstance(seen_payloads[1]["messages"][-1]["content"], str)
    assert "图片画面记忆参考" in seen_payloads[1]["messages"][-2]["content"]


@pytest.mark.asyncio
async def test_v170_l3_hybrid_recall_relevance_and_semi_isolation(db_session):
    db_session.add(QQIdentity(qq_id=3001, nickname="小明", private_profile={"secret": "私聊工资条"}))
    db_session.add_all(
        [
            MemoryEpisode(
                scope="public_group",
                group_id=606,
                qq_id=3001,
                title="普通日常",
                content="昨天群里随便聊了点天气",
                importance=0.95,
                created_at=datetime.now(timezone.utc) - timedelta(days=10),
            ),
            MemoryEpisode(
                scope="public_group",
                group_id=606,
                qq_id=3001,
                title="原神深渊攻略",
                content="小明昨晚用那维莱特满星通关了深渊十二层",
                importance=0.60,
                embedding_json=[1.0, 0.0, 0.0],
            ),
            MemoryEpisode(
                scope="private_dm",
                group_id=None,
                qq_id=3001,
                title="私密烦恼",
                content="深渊氪金太多不敢告诉群友",
                importance=0.99,
            ),
        ]
    )
    await db_session.commit()

    mem = MemoryService()
    ctx = await mem.assemble_context(
        db_session,
        scene="group",
        sender_qq=3001,
        group_id=606,
        query_text="深渊十二层",
        query_embedding=[0.95, 0.05, 0.0],
    )
    titles = [ep["title"] for ep in ctx["l3_episodes"]]
    assert titles[0] == "原神深渊攻略"
    assert "私密烦恼" not in titles
    assert "private_profile" not in ctx["l4_acquaintance"]
    assert ctx["l3_recall_meta"]["mode"] == "hybrid"


@pytest.mark.asyncio
async def test_v170_backup_dry_run_and_atomic_restore_api(tmp_path):
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-bk",
        snowluma_http_url="",
        admin_default_username="admin",
        admin_default_password="RestorePassword123!",
    )
    app = create_app(settings_override=settings, force_memory_store=True)

    snapshot = {
        "project": "EchoSea",
        "exported_at": "2026-11-26T10:00:00+08:00",
        "identities": [{"qq_id": 777001, "nickname": "恢复测试君", "relationship_level": 50, "known_names": ["小七"]}],
        "episodes": [{"scope": "public_group", "group_id": 8801, "qq_id": 777001, "title": "快照重逢", "content": "一起打过 Boss", "importance": 0.8}],
        "expressions": [{"group_id": 8801, "phrase": "典中典", "meaning": "太经典", "status": "approved"}],
        "stickers": [{"phash": "abcd1234abcd1234", "source_url": "https://img.example.com/s.png", "emotion_tags": ["笑"], "status": "approved"}],
        "personas": [],
        "hot_sources": [{"name": "测试RSS", "category": "tech", "feed_url": "https://example.com/rss", "is_enabled": True}],
    }
    gz_bytes = gzip.compress(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))

    with TestClient(app) as client:
        tok = client.post("/api/auth/login", json={"username": "admin", "password": "RestorePassword123!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {tok}"}

        # 1. Dry-run validates without mutating DB
        r_dry = client.post(
            "/api/ops/backups/restore?dry_run=true",
            headers=headers,
            files={"file": ("snap.json.gz", gz_bytes, "application/gzip")},
        )
        assert r_dry.status_code == 200 and r_dry.json()["dry_run"] is True
        assert r_dry.json()["counts"]["identities"] == 1
        assert len(client.get("/api/memory/identities", headers=headers).json()) == 0

        # 2. Execute restore imports records cleanly
        r_exec = client.post(
            "/api/ops/backups/restore?dry_run=false",
            headers=headers,
            files={"file": ("snap.json.gz", gz_bytes, "application/gzip")},
        )
        assert r_exec.status_code == 200 and r_exec.json()["restored"]["identities"] == 1
        ids = client.get("/api/memory/identities", headers=headers).json()
        assert len(ids) == 1 and ids[0]["nickname"] == "恢复测试君"

        # 3. Corrupted payload rejected with 400
        r_bad = client.post(
            "/api/ops/backups/restore?dry_run=true",
            headers=headers,
            files={"file": ("bad.json", b'{"project":"Other"}', "application/json")},
        )
        assert r_bad.status_code == 400


@pytest.mark.asyncio
async def test_v170_web_search_calibration_10_samples():
    """10-sample calibration: tool-calling triggers on live facts/URLs, stays conversational on casual banter."""
    samples = [
        ("今天成都天气怎么样", True),
        ("DeepSeek 最新开源模型发了吗", True),
        ("帮我看看这个链接里写了啥 https://example.com/news", True),
        ("今晚英超阿森纳几点踢", True),
        ("现在黄金多少钱一克了", True),
        ("今天累死了摸会鱼", False),
        ("哈哈哈哈笑死我了", False),
        ("晚饭吃麻辣烫还是烧烤", False),
        ("小呆呆你昨晚是不是又熬夜了", False),
        ("周一不想上班好困啊", False),
    ]
    assert len(samples) == 10

    async def calibrated_llm(_provider, _key, messages, _t, _max, tools=None, **_kw):
        user_msg = str(messages[-1]["content"])
        if any(k in user_msg for k in ("天气", "最新", "https://", "几点", "多少钱")):
            assert tools and any(t["function"]["name"] == "web_search" for t in tools)
            fn_name = "fetch_webpage" if "https://" in user_msg else "web_search"
            arg = json.dumps({"url": "https://example.com/news"} if fn_name == "fetch_webpage" else {"query": user_msg})
            return {"choices": [{"message": {"content": "等我查一下", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": fn_name, "arguments": arg}}]}}]}
        return {"choices": [{"message": {"content": "确实哈哈，早点下班摸鱼"}}]}

    hub = ModelHubService("k-cal", AlertService("k-cal"), http_caller=calibrated_llm)
    for text, expect_tool in samples:
        res = await simulated_single_turn(hub, text)
        assert bool(res.tool_calls) is expect_tool


async def simulated_single_turn(hub: ModelHubService, text: str):
    class _FakeSess:
        def add(self, _obj):
            pass

        async def flush(self):
            pass

    p = SimpleNamespace(
        id=1,
        slot="main_brain",
        role="primary",
        name="CalBrain",
        protocol="openai_compatible",
        base_url="https://api.example.com/v1",
        model_name="deepseek-v4",
        api_key_enc="",
        extra_config={},
        rate_limit_rpm=60,
        timeout_seconds=15,
        price_input_per_1m=0.0,
        price_output_per_1m=0.0,
        is_enabled=True,
    )
    raw = await hub._http_caller(p, "k", [{"role": "user", "content": text}], 0.7, 300, tools=WEB_SEARCH_TOOLS)
    msg = raw["choices"][0]["message"]
    from app.core.model_hub import ChatCompletionResult

    return ChatCompletionResult(
        content=msg.get("content") or "",
        provider_name=p.name,
        model_name=p.model_name,
        tool_calls=msg.get("tool_calls") or [],
    )


@pytest.mark.asyncio
async def test_v180_schema_migrations_ops_limits_and_zero_build_frontend():
    # 1. Versioned schema_migrations tracking
    db = DatabaseManager()
    db.init("sqlite+aiosqlite:///:memory:")
    await db.create_all()
    migs = [("001_test.sql", "CREATE TABLE IF NOT EXISTS sample_v18 (id INTEGER PRIMARY KEY)")]
    first = await db.apply_versioned_migrations(migs)
    second = await db.apply_versioned_migrations(migs)
    assert first == ["001_test.sql"]
    assert second == []
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        await db.apply_versioned_migrations([("001_test.sql", "CREATE TABLE changed_sql (id INT)")])
    await db.dispose()

    # 2. 2GB compose resource caps, log rotation, and dev-requirements split
    compose_txt = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "mem_limit: 512m" in compose_txt
    assert "mem_limit: 448m" in compose_txt
    assert "shared_buffers=128MB" in compose_txt
    assert "maxmemory 48mb" in compose_txt
    assert "max-size: \"10m\"" in compose_txt

    prod_reqs = Path("requirements.txt").read_text(encoding="utf-8")
    dev_reqs = Path("requirements-dev.txt").read_text(encoding="utf-8")
    assert "pytest" not in prod_reqs
    assert "pytest==" in dev_reqs

    # 3. Frontend security checks: zero localStorage JWT, zero inline onclick, ESM modules served cleanly
    index_html = Path("frontend/index.html").read_text(encoding="utf-8")
    assert "onclick=" not in index_html
    assert 'type="module"' in index_html
    all_js = "\n".join(p.read_text(encoding="utf-8") for p in Path("frontend/js").glob("*.js"))
    assert "localStorage.getItem('companion_token')" not in all_js
    assert "localStorage.setItem('companion_token'" not in all_js
