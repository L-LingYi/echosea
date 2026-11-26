"""v1.1 Diagnostics, Jev System One Gatekeeper, Consciousness Sea Graph & Benchmark Tests."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dashboard import dashboard_overview
from app.config import Settings
from app.core.snowluma_adapter import SnowLumaAdapter
from app.main import create_app
from app.models.db import AlertEvent, SystemConfig, UsageMetric


@pytest.mark.asyncio
async def test_dashboard_excludes_reviewed_latency_without_changing_usage(db_session):
    async def spend(session):
        return 0.0

    runtime = SimpleNamespace(
        adapter=SimpleNamespace(status_snapshot=lambda: {}),
        store=SimpleNamespace(backend_name="memory"),
        plugin_manager=SimpleNamespace(list_loaded=lambda: []),
        model_hub=SimpleNamespace(get_today_spend_usd=spend, daily_budget_usd=0),
    )
    normal = UsageMetric(slot="gatekeeper", provider_name="demo", model_name="model", ttft_ms=200, prompt_tokens=9)
    slow = UsageMetric(slot="gatekeeper", provider_name="demo", model_name="model",
                       ttft_ms=40000, prompt_tokens=100, estimated_cost_usd=0.01)
    alert = AlertEvent(severity="warning", category="slow_response", title="Slow request", detail="")
    db_session.add_all([normal, slow, alert])
    await db_session.flush()

    before = await dashboard_overview(days=7, tz_offset_minutes=480, session=db_session, runtime=runtime)
    assert before["tokens"]["avg_ttft_ms"] == 20100
    assert before["tokens"]["p95_ttft_ms"] == 40000
    assert before["tokens"]["slow_response_alerts"] == 1

    db_session.add(SystemConfig(scope_type="global", scope_id="dashboard_latency", config_json={
        "excluded_usage_ids": [slow.id],
        "excluded_alert_ids": [alert.id],
    }))
    await db_session.flush()
    after = await dashboard_overview(days=7, tz_offset_minutes=480, session=db_session, runtime=runtime)
    assert after["tokens"]["avg_ttft_ms"] == after["tokens"]["p95_ttft_ms"] == 200
    assert after["tokens"]["slow_response_alerts"] == 0
    assert after["tokens"]["latency_excluded_calls"] == 1
    for field in ("prompt_tokens", "total_calls", "prompt_cache_hit_rate_pct"):
        assert after["tokens"][field] == before["tokens"][field]
    assert after["cost"] == before["cost"]
    assert after["recent_alerts"] == before["recent_alerts"]


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
        # 1. Verify health & mobile-responsive WebUI with Consciousness Sea
        h = client.get("/healthz").json()
        assert h["version"] == settings.app_version
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
        dash_after_tts = client.get("/api/dashboard/overview?days=7", headers=headers).json()
        assert dash_after_tts["tts"]["today_chars"] > 0
        assert dash_after_tts["tts"]["total_chars"] == dash_after_tts["tts"]["today_chars"]
        assert any(s["slot"] == "tts" and s.get("unit") == "chars" and s["chars"] > 0 for s in dash_after_tts["by_slot"])

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

        # 8. Consciousness Sea Memory Graph API (/api/memory/graph) & Episode CRUD
        graph = client.get("/api/memory/graph", headers=headers).json()
        node_layers = {n["layer"] for n in graph["nodes"]}
        assert "L5_self" in node_layers
        assert "L4_identity" in node_layers
        assert "group" in node_layers
        assert graph["stats"]["edge_count"] >= 2
        ident_node = next(n for n in graph["nodes"] if n["layer"] == "L4_identity")
        assert "episode_count" in ident_node["detail"]
        assert "private_episode_count" in ident_node["detail"]

        # Seed a MemoryEpisode and verify graph enrichment + PATCH/DELETE episode endpoints
        import asyncio
        from app.models.db import MemoryEpisode

        async def _seed_ep():
            async with app.state.db_manager.session_factory() as s:
                ep = MemoryEpisode(
                    scope="public_group",
                    group_id=7777,
                    qq_id=50001,
                    title="烧烤摊排队趣事",
                    content="阿泽说今晚烧烤摊排队两小时。",
                    importance=0.82,
                )
                s.add(ep)
                await s.commit()
                await s.refresh(ep)
                return ep.id

        ep_id = asyncio.run(_seed_ep())
        g2 = client.get("/api/memory/graph", headers=headers).json()
        ep_node = next(n for n in g2["nodes"] if n["id"] == f"ep_{ep_id}")
        assert ep_node["detail"]["id"] == ep_id
        assert ep_node["detail"]["qq_id"] == 50001
        p_node = next(n for n in g2["nodes"] if n["id"] == "qq_50001")
        assert p_node["detail"]["episode_count"] >= 1

        # Star & archive episode -> hidden by default, shown when include_archived=true
        patched = client.patch(
            f"/api/memory/episodes/{ep_id}",
            headers=headers,
            json={"is_anniversary": True, "is_archived": True},
        ).json()
        assert patched["ok"] is True and patched["is_anniversary"] is True and patched["is_archived"] is True
        g_no_arch = client.get("/api/memory/graph", headers=headers).json()
        assert all(n["id"] != f"ep_{ep_id}" for n in g_no_arch["nodes"])
        g_with_arch = client.get("/api/memory/graph?include_archived=true", headers=headers).json()
        assert any(n["id"] == f"ep_{ep_id}" for n in g_with_arch["nodes"])

        # Delete episode cleanly
        deleted = client.delete(f"/api/memory/episodes/{ep_id}", headers=headers).json()
        assert deleted["ok"] is True and deleted["id"] == ep_id

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


@pytest.mark.asyncio
async def test_dashboard_today_metrics_align_with_local_midnight(monkeypatch):
    from datetime import datetime, timezone
    from app.api import dashboard as dashboard_api
    from app.models.db import TTSDelivery, UsageMetric

    fixed_now_utc = datetime(2026, 11, 25, 16, 2, 57, tzinfo=timezone.utc)  # 2026-11-26 00:02:57+08:00

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now_utc if tz is timezone.utc else fixed_now_utc.astimezone(tz)

    monkeypatch.setattr(dashboard_api, "datetime", _FrozenDatetime)

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-tz",
        snowluma_http_url="",
        bot_qq=888888,
        admin_qq=999999,
        admin_default_username="admin",
        admin_default_password="TzTestPassword!",
    )
    app = create_app(settings_override=settings, force_memory_store=True)

    with TestClient(app) as client:
        async def _seed():
            async with app.state.db_manager.session_factory() as session:
                session.add_all(
                    [
                        # 2026-11-25 23:30+08:00 (yesterday locally, same UTC day)
                        UsageMetric(
                            slot="main_brain",
                            provider_name="p",
                            model_name="m",
                            prompt_tokens=1000,
                            completion_tokens=200,
                            cached_tokens=100,
                            estimated_cost_usd=0.5,
                            ttft_ms=100,
                            total_ms=100,
                            source="production",
                            success=True,
                            created_at=datetime(2026, 11, 25, 15, 30, 0, tzinfo=timezone.utc),
                        ),
                        # 2026-11-26 00:01+08:00 (today locally)
                        UsageMetric(
                            slot="main_brain",
                            provider_name="p",
                            model_name="m",
                            prompt_tokens=300,
                            completion_tokens=50,
                            cached_tokens=30,
                            estimated_cost_usd=0.15,
                            ttft_ms=80,
                            total_ms=80,
                            source="production",
                            success=True,
                            created_at=datetime(2026, 11, 25, 16, 1, 0, tzinfo=timezone.utc),
                        ),
                        TTSDelivery(
                            scene="group",
                            target_id=1001,
                            intent="answer_voice",
                            cleaned_text="昨天晚上的语音",
                            synthesis_status="synthesized",
                            onebot_status="accepted",
                            created_at=datetime(2026, 11, 25, 15, 30, 0, tzinfo=timezone.utc),
                        ),
                        TTSDelivery(
                            scene="group",
                            target_id=1001,
                            intent="answer_voice",
                            cleaned_text="今天凌晨",
                            synthesis_status="synthesized",
                            onebot_status="accepted",
                            created_at=datetime(2026, 11, 25, 16, 1, 0, tzinfo=timezone.utc),
                        ),
                    ]
                )
                await session.commit()

        await _seed()

        token = client.post("/api/auth/login", json={"username": "admin", "password": "TzTestPassword!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        dash_cst = client.get("/api/dashboard/overview?days=7&tz_offset_minutes=480", headers=headers).json()
        assert dash_cst["tokens"]["today_total_tokens"] == 350
        assert dash_cst["tokens"]["today_prompt_tokens"] == 300
        assert dash_cst["tokens"]["today_completion_tokens"] == 50
        assert dash_cst["tokens"]["today_cached_tokens"] == 30
        assert dash_cst["tokens"]["total_tokens"] == 1550
        assert dash_cst["tts"]["today_chars"] == len("今天凌晨")
        assert dash_cst["tts"]["total_chars"] == len("昨天晚上的语音") + len("今天凌晨")
        assert dash_cst["cost"]["today_spend_usd"] == pytest.approx(0.15)
        assert dash_cst["usage_series"]["daily"][-1]["date"] == "2026-11-26"
        assert dash_cst["usage_series"]["daily"][-1]["tokens"] == 350
        assert dash_cst["usage_series"]["daily"][-1]["tts_chars"] == len("今天凌晨")

        dash_utc = client.get("/api/dashboard/overview?days=7&tz_offset_minutes=0", headers=headers).json()
        assert dash_utc["tokens"]["today_total_tokens"] == 1550
        assert dash_utc["tts"]["today_chars"] == len("昨天晚上的语音") + len("今天凌晨")
        assert dash_utc["cost"]["today_spend_usd"] == pytest.approx(0.65)


@pytest.mark.asyncio
async def test_local_timezone_for_schedule_backup_and_webui_logs(db_session, monkeypatch, tmp_path):
    from datetime import datetime
    from pathlib import Path

    from app.config import resolve_timezone
    from app.core import backup_service as backup_mod
    from app.core import life_schedule_service as sched_mod
    from app.core import web_perception_service as web_mod
    from app.models.db import HotTopicSource

    # 2026-11-25 17:05:00 UTC == 2026-11-26 01:05:00 Asia/Shanghai
    fixed_shanghai = datetime(2026, 11, 26, 1, 5, 0, tzinfo=resolve_timezone("Asia/Shanghai"))
    monkeypatch.setattr(sched_mod, "local_now", lambda tz=None: fixed_shanghai)
    monkeypatch.setattr(web_mod, "local_now", lambda tz=None: fixed_shanghai)
    monkeypatch.setattr(backup_mod, "local_now", lambda tz=None: fixed_shanghai)

    # 1. LifeScheduleService defaults to local hour (01:05 -> sleeping), not UTC hour (17:05)
    sched = sched_mod.LifeScheduleService(timezone_name="Asia/Shanghai")
    state = await sched.sync_current_state(db_session)
    assert state.current_activity == "睡觉补觉中"
    assert state.diary_entry.startswith("01:00 ")

    # 2. WebPerceptionService DailyBriefing uses local date (2026-11-26), not UTC date (2026-11-25)
    db_session.add(HotTopicSource(name="TestFeed", category="tech", feed_url="https://example.com/rss", is_enabled=True))
    await db_session.flush()

    async def fake_feed(_url: str) -> str:
        return "<rss><channel><item><title>Local Date Item</title></item></channel></rss>"

    wp = web_mod.WebPerceptionService(master_key="k", feed_fetcher=fake_feed, timezone_name="Asia/Shanghai")
    briefing = await wp.refresh_daily_briefing(db_session)
    assert briefing is not None
    assert briefing.briefing_date.isoformat() == "2026-11-26"

    # 3. BackupService snapshot filename & exported_at use local timezone
    bk = backup_mod.BackupService(master_key="k", local_backup_dir=str(tmp_path), timezone_name="Asia/Shanghai")
    filename, _ = await bk._build_snapshot_bytes(db_session)
    assert filename == "echosea_backup_20261126_010500.json.gz"

    # 4. WebUI renders trace/update/alert/backup timestamps via formatLocalTime (no raw UTC slicing)
    api_js = Path("frontend/js/api.js").read_text(encoding="utf-8")
    all_js = "\n".join(p.read_text(encoding="utf-8") for p in Path("frontend/js").glob("*.js"))
    assert "function formatLocalTime(" in api_js
    assert "formatLocalTime(r.created_at)" in all_js
    assert "formatLocalTime(j.created_at)" in all_js
    assert "formatLocalTime(e.created_at)" in all_js
    assert "formatLocalTime(bkCfg.last_auto_backup_at, true)" in all_js
    assert ".slice(5,19).replace('T',' ')" not in all_js
    assert "last_auto_backup_at.slice(0,19)" not in all_js


