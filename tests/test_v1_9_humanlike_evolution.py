"""Comprehensive tests for EchoSea v1.9 humanlike behavior & controlled self-evolution upgrades."""
from __future__ import annotations

import asyncio
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.config import Settings
from app.core.alert_service import AlertService
from app.core.brain_service import CompanionPipeline
from app.core.event_bus import PluginEventBus
from app.core.gatekeeper import GatekeeperService
from app.core.identity_service import IdentityService
from app.core.memory_service import MemoryService
from app.core.model_hub import ModelHubService
from app.core.multimodal_service import MultimodalService
from app.core.profile_schema import format_acquaintance_brief, relationship_tier
from app.core.redis_store import StateStore
from app.core.reflection_service import ReflectionService
from app.core.snowluma_adapter import SnowLumaAdapter
from app.core.sticker_service import StickerService
from app.main import create_app
from app.models.db import (
    ChatMessage,
    EvolutionSuggestion,
    MemoryEpisode,
    OutboxDelivery,
    PersonaVersion,
    QQIdentity,
    SystemConfig,
)
from app.models.schemas import GatekeeperDecision, MessageEnvelope


def _make_png_bytes(c1: tuple[int, int, int], c2: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (32, 32), c1)
    for x in range(16, 32):
        for y in range(32):
            img.putpixel((x, y), c2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_v190_implicit_social_feedback_reinforces_memory_and_tunes_threshold(db_session):
    async def mock_llm(_p, _k, _msgs, _t, _m, **_kw):
        return {"choices": [{"message": {"content": "那维莱特确实猛，深渊割草了"}}]}

    store = StateStore(force_memory=True)
    hub = ModelHubService("k-fb", AlertService("k-fb"), http_caller=mock_llm)
    await hub.upsert_provider(
        db_session,
        {
            "slot": "main_brain",
            "role": "primary",
            "name": "Brain-FB",
            "protocol": "openai_compatible",
            "base_url": "https://api.example.com/v1",
            "model_name": "m1",
            "api_key": "k",
        },
    )
    mem = MemoryService()
    id_svc = IdentityService()
    await id_svc.upsert_from_message(db_session, qq_id=4001, nickname="阿明", group_id=7001)
    ep = MemoryEpisode(
        scope="public_group",
        group_id=7001,
        qq_id=4001,
        title="深渊战绩",
        content="阿明用那维莱特满星深渊",
        importance=0.60,
    )
    db_session.add(ep)
    await db_session.commit()

    pipe = CompanionPipeline(
        store=store,
        identity_service=id_svc,
        gatekeeper=GatekeeperService(store=store),
        memory_service=mem,
        model_hub=hub,
        adapter=SnowLumaAdapter(),
        event_bus=PluginEventBus(),
    )

    # 1. Bot replies to user (direct_pass) -> OutboxDelivery starts as pending
    env1 = MessageEnvelope(
        message_id="fb_u1",
        scene="group",
        group_id=7001,
        sender_qq=4001,
        sender_nickname="阿明",
        raw_text="小呆呆，昨晚深渊打得咋样",
        mentioned_qqs=[888888],
        self_qq=888888,
    )
    res1 = await pipe.process_envelope(db_session, env1)
    assert res1["handled"] == "direct_pass"
    delivery_id = res1["deliveries"][0]["id"]
    delivery = await db_session.get(OutboxDelivery, delivery_id)
    # Mark sent (simulate SnowLuma accepted/sent) so feedback loop tracks it
    delivery.status = "sent"
    assert delivery.feedback_status == "pending"
    assert delivery.gatekeeper_action == "direct_pass"
    assert delivery.trigger_msg_id == "fb_u1"
    await db_session.commit()

    # 2. Group member responds positively within 120s -> engaged + reinforces recalled L3 episode!
    env2 = MessageEnvelope(
        message_id="fb_u2",
        scene="group",
        group_id=7001,
        sender_qq=4002,
        sender_nickname="小红",
        raw_text="哈哈哈哈确实，太懂了",
        self_qq=888888,
    )
    await pipe.process_envelope(db_session, env2)
    await db_session.refresh(delivery)
    await db_session.refresh(ep)
    assert delivery.feedback_status == "engaged"
    assert ep.recall_count == 1
    assert ep.last_recalled_at is not None
    assert ep.importance == pytest.approx(0.63)

    # 3. Simulate 5 cold_drop/rejected deliveries in group 7002 -> nightly reflection tunes threshold +0.02 within [0.68, 0.88]
    now = datetime.now(timezone.utc)
    for idx in range(5):
        db_session.add(
            OutboxDelivery(
                scene="group",
                target_id=7002,
                payload_text=f"冷场消息{idx}",
                status="sent",
                feedback_status="pending",
                created_at=now - timedelta(minutes=10),
            )
        )
    db_session.add(SystemConfig(scope_type="group", scope_id="7002", config_json={"soft_entry_threshold": 0.78}))
    await db_session.commit()

    refl = ReflectionService(model_hub=hub)
    summary = await refl.run_nightly_reflection(db_session, now_utc=now)
    assert summary["cold_drops_finalized"] >= 5
    assert summary["thresholds_tuned"] >= 1
    assert summary["few_shot_candidates"] >= 1

    cfg_7002 = (
        await db_session.execute(
            select(SystemConfig).where(SystemConfig.scope_type == "group", SystemConfig.scope_id == "7002")
        )
    ).scalar_one()
    assert cfg_7002.config_json["soft_entry_threshold"] == pytest.approx(0.80)

    # Idempotent same-day re-run must not step twice
    summary2 = await refl.run_nightly_reflection(db_session, now_utc=now)
    assert summary2["thresholds_tuned"] == 0

    # Pending Few-Shot candidate was extracted for WebUI review
    fs = (
        await db_session.execute(
            select(EvolutionSuggestion).where(EvolutionSuggestion.suggestion_type == "few_shot")
        )
    ).scalar_one()
    assert fs.status == "pending"
    assert fs.content_json["user"] == "小呆呆，昨晚深渊打得咋样"


@pytest.mark.asyncio
async def test_v190_dual_track_4d_profile_anti_joke_ttl_pii_and_100char_brief(db_session):
    id_svc = IdentityService()
    mem = MemoryService()
    await id_svc.upsert_from_message(db_session, qq_id=5001, nickname="阿杰", group_id=8001)

    now = datetime.now(timezone.utc)
    # Single group mention of 露营 + recent state + sensitive PII phone number
    msgs = [
        MessageEnvelope(
            message_id="p_g1",
            scene="group",
            group_id=8001,
            sender_qq=5001,
            raw_text="我这周赶项目累死了，周末想去露营，我手机号13800138000",
            self_qq=888888,
        ),
        # Private DM message -> MUST go to private_profile ONLY, never public_profile!
        MessageEnvelope(
            message_id="p_dm1",
            scene="private",
            group_id=None,
            sender_qq=5001,
            raw_text="私下说我最近在找工作，别提加班，我天天熬夜写后端",
            self_qq=888888,
        ),
    ]
    for env in msgs:
        await mem.record_message(db_session, env, GatekeeperDecision(action="silent", target="group_open", score=0.2, reason="r"))

    hub = ModelHubService("k-prof", AlertService("k-prof"))
    refl = ReflectionService(model_hub=hub)
    await refl.run_nightly_reflection(db_session, now_utc=now)

    ident = await db_session.get(QQIdentity, 5001)
    # 1. Single mention of 露营 stays in candidate_clues, NOT promoted to stable_traits yet!
    assert "户外露营" not in ident.public_profile["stable_traits"]["interests"]
    assert any(c["value"] == "户外露营" and c["count"] == 1 for c in ident.public_profile["candidate_clues"])
    # 2. Recent state stored with 7-14d expires_at
    assert any("赶项目" in s["text"] for s in ident.public_profile["recent_states"])
    # 3. Private DM clues go ONLY to private_profile, NOT public_profile
    assert any("工作" in s["text"] for s in ident.private_profile["recent_states"])
    assert not any("工作" in s["text"] for s in ident.public_profile["recent_states"])
    assert "加班" in ident.private_profile["relationship"]["boundaries"]
    assert "加班" not in ident.public_profile["relationship"]["boundaries"]
    # 4. PII phone number must never appear in profile
    assert "13800138000" not in str(ident.public_profile)

    # Second mention of 露营 promotes candidate_clue -> stable_traits["interests"]
    env_confirm = MessageEnvelope(
        message_id="p_g2",
        scene="group",
        group_id=8001,
        sender_qq=5001,
        raw_text="下周继续去露营！",
        self_qq=888888,
    )
    await mem.record_message(db_session, env_confirm, GatekeeperDecision(action="silent", target="group_open", score=0.2, reason="r"))
    await refl.run_nightly_reflection(db_session, now_utc=now + timedelta(minutes=5))
    await db_session.refresh(ident)
    assert "户外露营" in ident.public_profile["stable_traits"]["interests"]
    assert "户外露营" in ident.public_profile["interests"]

    # 5. Group prompt brief <= 100 chars and NEVER leaks private_profile
    group_ctx = await mem.assemble_context(db_session, scene="group", sender_qq=5001, group_id=8001)
    brief_group = format_acquaintance_brief(group_ctx["l4_acquaintance"], scene="group", max_chars=100, now_utc=now)
    assert len(brief_group) <= 100
    assert "工作" not in brief_group
    assert "户外露营" in brief_group

    # 6. Expired recent_states (>14 days later) automatically filtered out!
    brief_expired = format_acquaintance_brief(
        group_ctx["l4_acquaintance"], scene="group", max_chars=100, now_utc=now + timedelta(days=15)
    )
    assert "赶项目" not in brief_expired
    assert relationship_tier(0)[0] == "stranger"
    assert relationship_tier(15)[0] == "acquaintance"
    assert relationship_tier(40)[0] == "close_pal"


@pytest.mark.asyncio
async def test_v190_expression_self_audit_48h_hint_system_prompt_redline_and_memory_metabolism(db_session):
    async def mock_refl_llm(_p, _k, _msgs, _t, _m, **_kw):
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"profile_clues":[],"slang_candidates":[],"episodes":[],"persona_advices":[{"title":"毒舌度建议","advice":"吐槽可以再犀利一丢丢","reason":"群友互动偏好互损"}]}'
                    }
                }
            ]
        }

    hub = ModelHubService("k-aud", AlertService("k-aud"), http_caller=mock_refl_llm)
    await hub.upsert_provider(
        db_session,
        {
            "slot": "reflection",
            "role": "primary",
            "name": "Refl",
            "protocol": "openai_compatible",
            "base_url": "https://api.example.com/v1",
            "model_name": "m-refl",
            "api_key": "k",
        },
    )
    id_svc = IdentityService()
    await id_svc.upsert_from_message(db_session, qq_id=6001, nickname="阿强", group_id=8100)
    await id_svc.upsert_from_message(db_session, qq_id=888888, nickname="小呆呆", group_id=8100)

    # Seed active persona (system_prompt MUST NEVER be mutated by reflection!)
    orig_prompt = "你是小呆呆，说话自然简短。"
    persona = PersonaVersion(version_tag="v1.9-test", bot_name="小呆呆", system_prompt=orig_prompt, is_active=True)
    db_session.add(persona)

    # Seed 10 verbose bot replies all starting with "确实"
    for i in range(10):
        db_session.add(
            ChatMessage(
                message_id=f"bot_v_{i}",
                scene="group",
                group_id=8100,
                sender_qq=888888,
                sender_display_name="小呆呆",
                raw_text=f"确实，关于这个问题我觉得可以从三个方面来认真分析一下，首先第一点就是这样子的呢第{i}条长回复内容",
                gatekeeper_decision="bot_reply",
            )
        )
    db_session.add(
        ChatMessage(
            message_id="u_trigger",
            scene="group",
            group_id=8100,
            sender_qq=6001,
            sender_display_name="阿强",
            raw_text="今天好累啊",
            gatekeeper_decision="silent",
        )
    )

    # Seed L3 episodes: one >30d unhit ordinary (should archive), one >30d anniversary (must NEVER archive)
    old_ts = datetime.now(timezone.utc) - timedelta(days=40)
    ep_old = MemoryEpisode(
        scope="public_group",
        group_id=8100,
        qq_id=6001,
        title="过气日常",
        content="四十天前聊过一次午饭",
        importance=0.45,
        is_anniversary=False,
        created_at=old_ts,
    )
    ep_anniv = MemoryEpisode(
        scope="public_group",
        group_id=8100,
        qq_id=6001,
        title="一周年纪念",
        content="重要纪念日不能归档",
        importance=0.45,
        is_anniversary=True,
        created_at=old_ts,
    )
    db_session.add_all([ep_old, ep_anniv])
    await db_session.commit()

    refl = ReflectionService(model_hub=hub)
    res = await refl.run_nightly_reflection(db_session)
    assert res["correction_hints_created"] == 1
    assert res["banned_phrase_candidates"] == 1
    assert res["persona_advices_created"] >= 1
    assert res["episodes_archived"] == 1

    await db_session.refresh(persona)
    await db_session.refresh(ep_old)
    await db_session.refresh(ep_anniv)
    assert persona.system_prompt == orig_prompt  # REDLINE!
    assert ep_old.is_archived is True
    assert ep_anniv.is_archived is False

    # Archived episode is excluded from prompt retrieval
    mem = MemoryService()
    ctx = await mem.assemble_context(db_session, scene="group", sender_qq=6001, group_id=8100, query_text="午饭")
    assert all(e["title"] != "过气日常" for e in ctx["l3_episodes"])


@pytest.mark.asyncio
async def test_v190_quoted_and_hot_image_on_demand_vision(db_session):
    png = _make_png_bytes((30, 30, 30), (240, 200, 100))
    mm = MultimodalService(image_fetcher=lambda _u: asyncio.sleep(0, result=png))
    stk = StickerService()
    id_svc = IdentityService()
    await id_svc.upsert_from_message(db_session, qq_id=7001, nickname="阿图", group_id=8200)

    # Record a message with an image, then reply to it with soft_entry
    db_session.add(
        ChatMessage(
            message_id="img_orig_1",
            scene="group",
            group_id=8200,
            sender_qq=7001,
            sender_display_name="阿图",
            raw_text="看这个",
            media_meta=[{"type": "image", "url": "https://img.example.com/quoted.png"}],
        )
    )
    await db_session.flush()

    pipe = CompanionPipeline(
        store=StateStore(force_memory=True),
        identity_service=id_svc,
        gatekeeper=GatekeeperService(store=StateStore(force_memory=True)),
        memory_service=MemoryService(),
        model_hub=ModelHubService("k-v", AlertService("k-v")),
        adapter=SnowLumaAdapter(),
        event_bus=PluginEventBus(),
        sticker_service=stk,
        multimodal_service=mm,
    )
    env_reply = MessageEnvelope(
        message_id="img_reply_2",
        scene="group",
        group_id=8200,
        sender_qq=7001,
        raw_text="哈哈这张图笑死",
        reply_to_msg_id="img_orig_1",
        self_qq=888888,
    )
    blocks, _hints, _phashes, _hits = await pipe._collect_vision_context(
        db_session,
        env_reply,
        GatekeeperDecision(action="soft_entry", target="group_open", score=0.8, reason="soft"),
    )
    assert len(blocks) == 1

    # Unquoted cold image on soft_entry is skipped; high-heat sticker image IS decoded
    env_cold = MessageEnvelope(
        message_id="img_cold_3",
        scene="group",
        group_id=8200,
        sender_qq=7001,
        raw_text="发张冷图",
        media_meta=[{"type": "image", "url": "https://img.example.com/cold.png"}],
        self_qq=888888,
    )
    blocks_cold, *_ = await pipe._collect_vision_context(
        db_session,
        env_cold,
        GatekeeperDecision(action="soft_entry", target="group_open", score=0.8, reason="soft"),
    )
    assert blocks_cold == []

    await stk.observe_group_image(db_session, group_id=8200, phash="1111222233334444", source_url="https://img.example.com/hot.png")
    await stk.observe_group_image(db_session, group_id=8200, phash="1111222233334444", source_url="https://img.example.com/hot.png")
    env_hot = MessageEnvelope(
        message_id="img_hot_4",
        scene="group",
        group_id=8200,
        sender_qq=7001,
        raw_text="又是这张图",
        media_meta=[{"type": "image", "url": "https://img.example.com/hot.png"}],
        self_qq=888888,
    )
    blocks_hot, *_ = await pipe._collect_vision_context(
        db_session,
        env_hot,
        GatekeeperDecision(action="soft_entry", target="group_open", score=0.8, reason="soft"),
    )
    assert len(blocks_hot) == 1


def test_v190_webui_apis_4d_identity_edit_and_suggestion_review():
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        snowluma_access_token="tok-v19",
        snowluma_http_url="",
        admin_default_username="admin",
        admin_default_password="EvoPassword123!",
    )
    app = create_app(settings_override=settings, force_memory_store=True)

    with TestClient(app) as client:
        tok = client.post("/api/auth/login", json={"username": "admin", "password": "EvoPassword123!"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {tok}"}

        import asyncio as _aio

        async def _seed():
            async with app.state.db_manager.session_factory() as s:
                s.add(QQIdentity(qq_id=91001, nickname="测试群友", relationship_level=12))
                s.add(
                    EvolutionSuggestion(
                        suggestion_type="few_shot",
                        status="pending",
                        group_id=100,
                        title="高赞回复候选",
                        content_json={"user": "今天摸鱼吗", "assistant": "摸，必须摸到下班"},
                        reason="高互动",
                    )
                )
                s.add(
                    EvolutionSuggestion(
                        suggestion_type="banned_phrase",
                        status="pending",
                        title="高频口癖词：哟呵",
                        content_json={"phrase": "哟呵"},
                        reason="高频口癖",
                    )
                )
                await s.commit()

        _aio.run(_seed())

        # 1. Edit 4D profile via PATCH /api/memory/identities/{qq_id}
        r_patch = client.patch(
            "/api/memory/identities/91001",
            headers=headers,
            json={
                "relationship_level": 40,
                "public_profile": {
                    "relationship": {"preferred_address": "老九", "interaction_style": "爱互损", "boundaries": []},
                    "stable_traits": {"interests": ["摄影"], "expertise": ["Python"], "schedule_habits": ["夜猫子"]},
                },
            },
        )
        assert r_patch.status_code == 200
        assert r_patch.json()["relationship_tier"] == "close_pal"

        ids = client.get("/api/memory/identities", headers=headers).json()
        assert ids[0]["relationship_tier_label"] == "老熟人"
        assert ids[0]["public_profile"]["stable_traits"]["expertise"] == ["Python"]

        # 2. Review pending Few-Shot & banned phrase via /api/persona/suggestions
        sugs = client.get("/api/persona/suggestions?status=pending", headers=headers).json()
        assert len(sugs) == 2
        for sg in sugs:
            rv = client.post(f"/api/persona/suggestions/{sg['id']}/review", headers=headers, json={"action": "approve"})
            assert rv.status_code == 200 and rv.json()["status"] == "approved"

        versions = client.get("/api/persona/versions", headers=headers).json()
        active_v = next(v for v in versions if v["is_active"])
        assert any(ex["assistant"] == "摸，必须摸到下班" for ex in active_v["few_shot_examples"])
        assert "哟呵" in active_v["banned_phrases"]

        # 3. Bot's own QQ (recorded via bot_reply even when settings.bot_qq=0) MUST NOT appear in /api/memory/graph or /api/memory/identities
        async def _seed_bot():
            async with app.state.db_manager.session_factory() as s:
                s.add(QQIdentity(qq_id=2164459727, nickname="小呆呆", group_cards={"486135657": "小呆呆"}))
                s.add(
                    ChatMessage(
                        message_id="bot_self_1",
                        scene="group",
                        group_id=486135657,
                        sender_qq=2164459727,
                        sender_display_name="小呆呆",
                        raw_text="哈哈确实",
                        gatekeeper_decision="bot_reply",
                    )
                )
                await s.commit()

        _aio.run(_seed_bot())
        ids_after = client.get("/api/memory/identities", headers=headers).json()
        assert all(i["qq_id"] != 2164459727 for i in ids_after)
        graph = client.get("/api/memory/graph", headers=headers).json()
        assert all(n["id"] != "qq_2164459727" for n in graph["nodes"])
        assert any(n["id"] == "group_486135657" for n in graph["nodes"])


@pytest.mark.asyncio
async def test_v190_gatekeeper_interruption_decay_and_other_user_clears_attention(state_store):
    gk = GatekeeperService(
        store=state_store,
        soft_entry_threshold=0.78,
        attention_hot_seconds=60.0,
        attention_decay_seconds=240.0,
    )

    # 1. User A @bot -> activates 0-60s hot attention window
    d0 = await gk.evaluate(
        MessageEnvelope(
            message_id="int_1",
            scene="group",
            group_id=50001,
            sender_qq=1001,
            raw_text="笑死，你别这样说",
            mentioned_qqs=[888888],
            self_qq=888888,
        ),
        bot_name="小呆呆",
        aliases=["呆呆"],
    )
    assert d0.action == "direct_pass"

    # 2. Third-party User B posts game screenshots + asks a question ("是这个游戏吗")
    d1 = await gk.evaluate(
        MessageEnvelope(
            message_id="int_2",
            scene="group",
            group_id=50001,
            sender_qq=1002,
            raw_text="是这个游戏吗",
            media_meta=[{"type": "image", "url": "https://img.example.com/game1.png"}],
            self_qq=888888,
        ),
        bot_name="小呆呆",
        aliases=["呆呆"],
    )
    assert d1.action == "silent"

    # 3. User A replies "我不知道，我不玩" (answering B's image question, NOT bot)
    # Hot 0.35 discount is revoked due to third-party hook -> ambiguous 0.45 score stays silent!
    async def _ambiguous_scorer(_env, _target):
        return {"target": "group_open", "score": 0.45, "reason": "ambiguous after interruption"}

    d2 = await gk.evaluate(
        MessageEnvelope(
            message_id="int_3",
            scene="group",
            group_id=50001,
            sender_qq=1001,
            raw_text="我不知道，我不玩",
            self_qq=888888,
        ),
        bot_name="小呆呆",
        aliases=["呆呆"],
        llm_scorer=_ambiguous_scorer,
    )
    assert d2.action == "silent"
    assert "interrupted" in d2.reason

    # 4. If gatekeeper LLM detects target == 'other_user', personal hot attention is cleared immediately
    async def _other_user_scorer(_env, _target):
        return {"target": "other_user", "score": 0.1, "reason": "talking to B"}

    d3 = await gk.evaluate(
        MessageEnvelope(
            message_id="int_4",
            scene="group",
            group_id=50001,
            sender_qq=1001,
            raw_text="你自己看截图",
            self_qq=888888,
        ),
        bot_name="小呆呆",
        aliases=["呆呆"],
        llm_scorer=_other_user_scorer,
    )
    assert d3.action == "silent"
    assert d3.target == "other_user"
    assert await state_store.get_user_attention_age_seconds(50001, 1001) is None


@pytest.mark.asyncio
async def test_v190_l1_bubble_storage_identity_anchor_and_casual_punctuation(db_session):
    from app.core.prompt_builder import format_casual_bubbles

    # Whitelist guard on casual punctuation cleanup
    assert format_casual_bubbles(["行吧。"], "行吧。") == ["行吧"]
    assert format_casual_bubbles(["“真的假的。”"], "“真的假的。”") == ["真的假的"]
    assert format_casual_bubbles(["已升级到 v1.9.0。"], "已升级到 v1.9.0。") == ["已升级到 v1.9.0"]
    assert format_casual_bubbles(["看这个 https://example.com/a。"], "看这个 https://example.com/a。") == [
        "看这个 https://example.com/a。"
    ]
    long_txt = "这是一段超过八十字的认真解答内容。" * 6
    assert format_casual_bubbles([long_txt], long_txt) == [long_txt]

    captured_prompts: list[list[dict]] = []
    turn_idx = 0

    async def mock_llm(_p, _k, msgs, _t, _m, **_kw):
        nonlocal turn_idx
        turn_idx += 1
        captured_prompts.append(msgs)
        if turn_idx == 1:
            return {"choices": [{"message": {"content": "哎呀今天群里怎么突然这么热闹呀。\n感觉大家现在心情都很不错呢。"}}]}
        return {"choices": [{"message": {"content": "我就爱插嘴"}}]}

    store = StateStore(force_memory=True)
    hub = ModelHubService("k-v19-fmt", AlertService("k-v19-fmt"), http_caller=mock_llm)
    await hub.upsert_provider(
        db_session,
        {
            "slot": "main_brain",
            "role": "primary",
            "name": "Brain-Fmt",
            "protocol": "openai_compatible",
            "base_url": "https://api.example.com/v1",
            "model_name": "m1",
            "api_key": "k",
        },
    )
    pipe = CompanionPipeline(
        store=store,
        identity_service=IdentityService(),
        gatekeeper=GatekeeperService(store=store),
        memory_service=MemoryService(),
        model_hub=hub,
        adapter=SnowLumaAdapter(http_url=""),
        event_bus=PluginEventBus(),
    )

    # Turn 1: multi-bubble reply with trailing Chinese periods -> stripped & stored with '\n'
    res1 = await pipe.process_envelope(
        db_session,
        MessageEnvelope(
            message_id="fmt_1",
            scene="group",
            group_id=60001,
            sender_qq=2001,
            sender_nickname="老六",
            raw_text="小呆呆怎么不说话",
            mentioned_qqs=[888888],
            self_qq=888888,
        ),
    )
    assert res1["bubbles"] == ["哎呀今天群里怎么突然这么热闹呀", "感觉大家现在心情都很不错呢"]
    bot_msg = (
        await db_session.execute(select(ChatMessage).where(ChatMessage.gatekeeper_decision == "bot_reply"))
    ).scalar_one()
    assert bot_msg.raw_text == "哎呀今天群里怎么突然这么热闹呀\n感觉大家现在心情都很不错呢"

    # Turn 2: prompt static prefix has self-identity anchor, and L1 renders [我(小呆呆)] per bubble
    await pipe.process_envelope(
        db_session,
        MessageEnvelope(
            message_id="fmt_2",
            scene="group",
            group_id=60001,
            sender_qq=2001,
            sender_nickname="老六",
            raw_text="和AI吵起来了",
            mentioned_qqs=[888888],
            self_qq=888888,
        ),
    )
    sys_static = captured_prompts[1][0]["content"]
    sys_dynamic = captured_prompts[1][-2]["content"]
    assert "「AI」「机器人」" in sys_static
    assert "第二人称「你」" in sys_static
    assert "[我(小呆呆)]: 哎呀今天群里怎么突然这么热闹呀" in sys_dynamic
    assert "[我(小呆呆)]: 感觉大家现在心情都很不错呢" in sys_dynamic


@pytest.mark.asyncio
async def test_v190_gatekeeper_image_visibility_and_async_caption(db_session):
    png_bytes = _make_png_bytes((30, 60, 200), (240, 200, 40))
    gk_states: list[str] = []

    async def mock_llm(provider, _k, msgs, _t, _m, **_kw):
        if provider.slot == "gatekeeper":
            gk_states.append(msgs[-1]["content"])
            return {"choices": [{"message": {"content": '{"target":"other_user","score":0.1,"reason":"discussing image"}'}}]}
        return {"choices": [{"message": {"content": "原神抽卡歪了截图"}}]}

    store = StateStore(force_memory=True)
    hub = ModelHubService("k-v19-vis", AlertService("k-v19-vis"), http_caller=mock_llm)
    for slot in ("main_brain", "gatekeeper"):
        await hub.upsert_provider(
            db_session,
            {
                "slot": slot,
                "role": "primary",
                "name": f"P-{slot}",
                "protocol": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "model_name": "m1",
                "api_key": "k",
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

    # 1. User B posts an image-only message (raw_text == "") -> silent, then background caption populates ImageVisionCache
    img_url = "https://img.example.com/gacha_pull.png"
    res_img = await pipe.process_envelope(
        db_session,
        MessageEnvelope(
            message_id="vis_img_1",
            scene="group",
            group_id=60002,
            sender_qq=3002,
            sender_nickname="路人B",
            raw_text="",
            media_meta=[{"type": "image", "url": img_url}],
            self_qq=888888,
        ),
    )
    assert res_img["handled"] == "silent"
    captioned = await pipe.caption_image_items([{"type": "image", "url": img_url}], session=db_session)
    assert captioned == 1

    # 2. Next message enters Gatekeeper LLM -> recent context includes '[图片: 原神抽卡歪了截图]' instead of dropping!
    res_follow = await pipe.process_envelope(
        db_session,
        MessageEnvelope(
            message_id="vis_txt_2",
            scene="group",
            group_id=60002,
            sender_qq=3001,
            sender_nickname="路人A",
            raw_text="我不玩这个",
            self_qq=888888,
        ),
    )
    assert res_follow["handled"] == "silent"
    assert any("[图片: 原神抽卡歪了截图]" in st for st in gk_states)


@pytest.mark.asyncio
async def test_v190_debouncer_max_wait_and_generation_cancellation(db_session):
    from app.core.redis_store import MessageDebouncer

    # 1. MessageDebouncer respects max_wait_seconds cap even on continuous pushes
    flushed_batches: list[list[str]] = []
    deb = MessageDebouncer(window_seconds=0.35, max_batch_size=8, max_wait_seconds=0.20)

    async def _on_batch(batch):
        flushed_batches.append(batch)

    await deb.push("k1", "a", _on_batch)
    await asyncio.sleep(0.10)
    await deb.push("k1", "b", _on_batch)
    await asyncio.sleep(0.22)
    assert flushed_batches == [["a", "b"]]

    # 2. generation_id cancels stale remaining bubbles during inter-bubble sleep without blocking group lock
    async def mock_llm(_p, _k, _msgs, _t, _m, **_kw):
        return {"choices": [{"message": {"content": "第一句冒泡先把话说清楚啦。\n第二句冒泡肯定长过二十八字所以不会合并在一起对吧哈哈哈"}}]}

    store = StateStore(force_memory=True)
    hub = ModelHubService("k-v19-gen", AlertService("k-v19-gen"), http_caller=mock_llm)
    await hub.upsert_provider(
        db_session,
        {
            "slot": "main_brain",
            "role": "primary",
            "name": "Brain-Gen",
            "protocol": "openai_compatible",
            "base_url": "https://api.example.com/v1",
            "model_name": "m1",
            "api_key": "k",
        },
    )
    pipe = CompanionPipeline(
        store=store,
        identity_service=IdentityService(),
        gatekeeper=GatekeeperService(store=store),
        memory_service=MemoryService(),
        model_hub=hub,
        adapter=SnowLumaAdapter(http_url=""),
        event_bus=PluginEventBus(),
        bubble_delay_min_seconds=0.25,
        bubble_delay_max_seconds=0.25,
    )

    task1 = asyncio.create_task(
        pipe.process_envelope(
            db_session,
            MessageEnvelope(
                message_id="gen_1",
                scene="group",
                group_id=60003,
                sender_qq=4001,
                sender_nickname="小王",
                raw_text="小呆呆说两句",
                mentioned_qqs=[888888],
                self_qq=888888,
            ),
        )
    )
    # Wait briefly so task1 finishes LLM and enters inter-bubble sleep outside the group lock
    await asyncio.sleep(0.08)
    # Incoming silent message in same group increments generation_id while task1 is sleeping
    res2 = await pipe.process_envelope(
        db_session,
        MessageEnvelope(
            message_id="gen_2",
            scene="group",
            group_id=60003,
            sender_qq=4002,
            sender_nickname="小李",
            raw_text="打断一下",
            self_qq=888888,
        ),
    )
    assert res2["handled"] == "silent"
    res1 = await task1
    assert res1["cancelled_stale"] is True
    assert len(res1["bubbles"]) == 1
