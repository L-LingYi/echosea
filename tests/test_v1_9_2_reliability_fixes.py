"""Regression tests for EchoSea v1.9.2 reliability & memory fixes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.alert_service import AlertService
from app.core.brain_service import CompanionPipeline
from app.core.event_bus import PluginEventBus
from app.core.gatekeeper import GatekeeperService
from app.core.identity_service import IdentityService
from app.core.memory_service import MemoryService
from app.core.model_hub import ModelHubService
from app.core.prompt_builder import build_prompt_messages
from app.core.reflection_service import ReflectionService
from app.core.snowluma_adapter import SnowLumaAdapter
from app.core.redis_store import StateStore
from app.core.sticker_service import StickerService
from app.models.db import (
    ChatMessage,
    MemoryEpisode,
    OutboxDelivery,
    PersonaVersion,
    QQIdentity,
    SystemConfig,
)
from app.models.schemas import GatekeeperDecision, MessageEnvelope


@pytest.mark.asyncio
async def test_v192_gateway_message_id_saved_per_bubble_and_quote_lookup_works(db_session):
    """Bot replies store real gateway message_id per sent bubble; failed bubbles skip ChatMessage."""
    store = StateStore(force_memory=True)
    await store.connect()
    hub = ModelHubService("k-quote", AlertService("k-quote"))

    async def _fake_chat(provider, api_key, messages, temperature, max_tokens):
        return {"choices": [{"message": {"content": "第一句气泡测试内容有点长这样会拆开。\n第二句气泡内容也够长用来测试。"}}]}

    hub._http_caller = _fake_chat
    await hub.upsert_provider(
        db_session,
        {
            "slot": "main_brain",
            "role": "primary",
            "name": "mock-brain",
            "base_url": "http://mock.local/v1",
            "model_name": "mock",
            "api_key": "sk-mock",
        },
    )

    adapter = SnowLumaAdapter(http_url="http://mock-snowluma", http_access_token="tok")
    seq_ids = iter(["90001", "90002"])

    async def _fake_send_http(action, params):
        return next(seq_ids)

    adapter._send_http = _fake_send_http  # type: ignore[method-assign]

    pipe = CompanionPipeline(
        store=store,
        identity_service=IdentityService(),
        gatekeeper=GatekeeperService(store=store),
        memory_service=MemoryService(),
        model_hub=hub,
        adapter=adapter,
        event_bus=PluginEventBus(),
        bubble_delay_min_seconds=0.0,
        bubble_delay_max_seconds=0.0,
    )

    env = MessageEnvelope(
        message_id="u_100",
        scene="group",
        group_id=9100,
        sender_qq=5100,
        sender_nickname="阿强",
        raw_text="小呆呆说两句",
        mentioned_qqs=[888888],
        self_qq=888888,
    )
    res = await pipe.process_envelope(db_session, env)
    await db_session.commit()

    bot_msgs = list(
        (
            await db_session.execute(
                select(ChatMessage)
                .where(ChatMessage.group_id == 9100, ChatMessage.gatekeeper_decision == "bot_reply")
                .order_by(ChatMessage.id.asc())
            )
        )
        .scalars()
        .all()
    )
    assert len(bot_msgs) >= 1
    assert bot_msgs[0].message_id == "90001"
    assert bot_msgs[0].request_id == res["request_id"]

    # Quoting the bot's real gateway message_id now resolves quoted_text and quoted_sender_qq!
    quoted_env = await adapter.normalize_event(
        {
            "post_type": "message",
            "message_type": "group",
            "self_id": 888888,
            "group_id": 9100,
            "user_id": 5100,
            "message_id": "u_101",
            "sender": {"nickname": "阿强"},
            "message": [
                {"type": "reply", "data": {"id": "90001"}},
                {"type": "text", "data": {"text": "你刚说啥"}},
            ],
        },
        session=db_session,
    )
    assert quoted_env is not None
    assert quoted_env.quoted_sender_qq == 888888
    assert quoted_env.quoted_text == bot_msgs[0].raw_text

    # Failed delivery must NOT be persisted as a spoken ChatMessage
    async def _fail_send_http(action, params):
        raise RuntimeError("gateway down")

    adapter._send_http = _fail_send_http  # type: ignore[method-assign]
    env_fail = MessageEnvelope(
        message_id="u_102",
        scene="group",
        group_id=9100,
        sender_qq=5100,
        sender_nickname="阿强",
        raw_text="小呆呆再说一句",
        mentioned_qqs=[888888],
        self_qq=888888,
    )
    res_fail = await pipe.process_envelope(db_session, env_fail)
    await db_session.commit()
    failed_bot_msgs = list(
        (
            await db_session.execute(
                select(ChatMessage).where(ChatMessage.request_id == res_fail["request_id"])
            )
        )
        .scalars()
        .all()
    )
    assert failed_bot_msgs == []


@pytest.mark.asyncio
async def test_v192_legacy_and_direct_pass_excluded_from_soft_entry_threshold_tuning(db_session):
    """Pre-v1.9 rows (gatekeeper_action=None) never become cold_drop; direct_pass never tunes soft_entry_threshold."""
    now = datetime.now(timezone.utc)
    # 6 legacy pre-v1.9 rows (gatekeeper_action=None)
    for idx in range(6):
        db_session.add(
            OutboxDelivery(
                scene="group",
                target_id=9200,
                payload_text=f"legacy_{idx}",
                status="sent",
                feedback_status="pending",
                gatekeeper_action=None,
                created_at=now - timedelta(hours=2),
            )
        )
    # 6 direct_pass rows (user got their answer and naturally ended chat)
    for idx in range(6):
        db_session.add(
            OutboxDelivery(
                scene="group",
                target_id=9201,
                payload_text=f"direct_{idx}",
                status="sent",
                feedback_status="pending",
                gatekeeper_action="direct_pass",
                created_at=now - timedelta(minutes=15),
            )
        )
    db_session.add(SystemConfig(scope_type="group", scope_id="9200", config_json={"soft_entry_threshold": 0.78}))
    db_session.add(SystemConfig(scope_type="group", scope_id="9201", config_json={"soft_entry_threshold": 0.78}))
    await db_session.commit()

    refl = ReflectionService(model_hub=ModelHubService("k-fb", AlertService("k-fb")))
    summary = await refl.run_nightly_reflection(db_session, now_utc=now)
    # Only the 6 direct_pass rows finalize as cold_drop; legacy rows stay untouched; NEITHER group tunes threshold!
    assert summary["cold_drops_finalized"] == 6
    assert summary["thresholds_tuned"] == 0

    legacy_rows = list(
        (await db_session.execute(select(OutboxDelivery).where(OutboxDelivery.target_id == 9200))).scalars().all()
    )
    assert all(r.feedback_status == "pending" for r in legacy_rows)


@pytest.mark.asyncio
async def test_v192_reflection_isolates_group_private_parses_fenced_json_and_embeds_episodes(db_session):
    """Group & private reflection run separately, parse ```json``` fences, bind group_id, and compute embeddings."""
    id_svc = IdentityService()
    mem = MemoryService()
    await id_svc.upsert_from_message(db_session, qq_id=6101, nickname="群友A", group_id=9300)
    await id_svc.upsert_from_message(db_session, qq_id=6102, nickname="私聊B", group_id=None)

    await mem.record_message(
        db_session,
        MessageEnvelope(message_id="g_ep1", scene="group", group_id=9300, sender_qq=6101, sender_nickname="群友A", raw_text="我们约好周末去爬山"),
        GatekeeperDecision(action="silent", target="group_open", score=0.2, reason="r"),
    )
    await mem.record_message(
        db_session,
        MessageEnvelope(message_id="dm_ep1", scene="private", group_id=None, sender_qq=6102, sender_nickname="私聊B", raw_text="悄悄告诉你我准备辞职创业"),
        GatekeeperDecision(action="direct_pass", target="bot", score=1.0, reason="dm"),
    )
    await db_session.commit()

    hub = ModelHubService("k-refl", AlertService("k-refl"))

    async def _fake_refl_llm(provider, api_key, messages, temperature, max_tokens):
        prompt_text = messages[0]["content"]
        if "【私聊】" in prompt_text:
            # Deliberately omit scene/scope & wrap in markdown code fence
            return {
                "choices": [
                    {
                        "message": {
                            "content": '```json\n{"profile_clues":[{"qq_id":6102,"category":"interests","value":"创业"}],"episodes":[{"qq_id":6102,"title":"准备创业","content":"私聊B提到准备辞职创业"}]}\n```'
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": '```json\n{"profile_clues":[{"qq_id":6102,"category":"interests","value":"不应串入群聊"},{"qq_id":6101,"category":"interests","value":"爬山"}],"episodes":[{"title":"周末爬山约定","content":"群友A约好周末去爬山"}]}\n```'
                    }
                }
            ]
        }

    hub._http_caller = _fake_refl_llm

    async def _fake_embed(session, text):
        return [0.25, 0.75, 0.0, 0.5]

    hub.embed_text = _fake_embed  # type: ignore[method-assign]
    await hub.upsert_provider(
        db_session,
        {
            "slot": "reflection",
            "role": "primary",
            "name": "mock-refl",
            "base_url": "http://mock.local/v1",
            "model_name": "mock",
            "api_key": "sk-mock",
        },
    )

    refl = ReflectionService(model_hub=hub)
    summary = await refl.run_nightly_reflection(db_session)
    await db_session.commit()

    assert summary["episodes_created"] == 2

    # Private-only user 6102 must NOT have any clue leaked into public_profile
    ident_b = await db_session.get(QQIdentity, 6102)
    assert ident_b is not None
    assert ident_b.public_profile == {} or not ident_b.public_profile.get("candidate_clues")
    assert ident_b.private_profile.get("candidate_clues")

    # Group episode automatically bound to group_id=9300 with embedding_json, and recallable!
    eps = list((await db_session.execute(select(MemoryEpisode))).scalars().all())
    pub_ep = next(e for e in eps if e.scope == "public_group")
    priv_ep = next(e for e in eps if e.scope == "private_dm")
    assert pub_ep.group_id == 9300
    assert pub_ep.embedding_json == [0.25, 0.75, 0.0, 0.5]
    assert priv_ep.group_id is None
    assert priv_ep.qq_id == 6102

    ctx = await mem.assemble_context(
        db_session,
        scene="group",
        sender_qq=6101,
        group_id=9300,
        query_text="周末爬山",
        query_embedding=[0.25, 0.75, 0.0, 0.5],
    )
    assert len(ctx["l3_episodes"]) == 1
    assert ctx["l3_episodes"][0]["title"] == "周末爬山约定"


@pytest.mark.asyncio
async def test_v192_private_dm_history_and_quote_preserve_bot_previous_reply(db_session):
    """In private DM, second turn sees bot's first reply in L1 history and can quote bot's reply."""
    store = StateStore(force_memory=True)
    await store.connect()
    hub = ModelHubService("k-dm", AlertService("k-dm"))
    captured_prompts: list[list[dict]] = []

    async def _fake_chat(provider, api_key, messages, temperature, max_tokens):
        captured_prompts.append(messages)
        if len(captured_prompts) == 1:
            return {"choices": [{"message": {"content": "推荐两个：第一火锅，第二烧烤"}}]}
        return {"choices": [{"message": {"content": "第二个就是烧烤呀"}}]}

    hub._http_caller = _fake_chat
    await hub.upsert_provider(
        db_session,
        {
            "slot": "main_brain",
            "role": "primary",
            "name": "mock-brain",
            "base_url": "http://mock.local/v1",
            "model_name": "mock",
            "api_key": "sk-mock",
        },
    )

    adapter = SnowLumaAdapter(http_url="http://mock-snowluma", http_access_token="tok")
    msg_ids = iter(["dm_bot_1", "dm_bot_2"])

    async def _fake_send_http(action, params):
        return next(msg_ids)

    adapter._send_http = _fake_send_http  # type: ignore[method-assign]
    pipe = CompanionPipeline(
        store=store,
        identity_service=IdentityService(),
        gatekeeper=GatekeeperService(store=store),
        memory_service=MemoryService(),
        model_hub=hub,
        adapter=adapter,
        event_bus=PluginEventBus(),
    )

    # Turn 1
    await pipe.process_envelope(
        db_session,
        MessageEnvelope(
            message_id="dm_u1",
            scene="private",
            sender_qq=7100,
            sender_nickname="小明",
            raw_text="晚饭推荐两个",
            self_qq=888888,
        ),
    )
    await db_session.commit()

    # Turn 2: follow-up quoting bot's previous DM message
    env2 = await adapter.normalize_event(
        {
            "post_type": "message",
            "message_type": "private",
            "self_id": 888888,
            "user_id": 7100,
            "message_id": "dm_u2",
            "sender": {"nickname": "小明"},
            "message": [
                {"type": "reply", "data": {"id": "dm_bot_1"}},
                {"type": "text", "data": {"text": "你刚提的第二个呢"}},
            ],
        },
        session=db_session,
    )
    assert env2 is not None
    assert env2.quoted_sender_qq == 888888
    assert "第二烧烤" in (env2.quoted_text or "")

    await pipe.process_envelope(db_session, env2)
    await db_session.commit()

    second_prompt_blob = "\n".join(str(m.get("content", "")) for m in captured_prompts[1])
    assert "推荐两个：第一火锅，第二烧烤" in second_prompt_blob


@pytest.mark.asyncio
async def test_v192_sticker_db_matching_and_shared_prompt_injection(db_session):
    """Sticker matching avoids unrelated fallback and shared prompt injects top approved tags."""
    stk = StickerService()
    await stk.import_manual(
        db_session,
        [
            {"phash": "aaaa1111aaaa1111", "source_url": "https://img/laugh.png", "emotion_tags": ["大笑"]},
            {"phash": "bbbb2222bbbb2222", "source_url": "https://img/speechless.png", "emotion_tags": ["无语"]},
        ],
    )
    await db_session.commit()

    picked = await stk.pick_sticker_for_tag(db_session, "无语")
    assert picked is not None
    assert picked.phash == "bbbb2222bbbb2222"

    # Unrelated tag returns None instead of spamming rows[0]
    assert await stk.pick_sticker_for_tag(db_session, "完全不存在的标签") is None

    top_tags = await stk.get_top_approved_tags(db_session, limit=8)
    assert "大笑" in top_tags and "无语" in top_tags

    persona = PersonaVersion(
        version_tag="custom_no_sticker_hint",
        bot_name="小呆呆",
        aliases=["呆呆"],
        system_prompt="自定义人设，没有写发图说明。",
        few_shot_examples=[],
        banned_phrases=[],
        is_active=True,
    )
    msgs = build_prompt_messages(
        persona=persona,
        memory_ctx={
            "l1_working": [],
            "l1_group_to_dm_bridge": [],
            "l2_ambient_summaries": [],
            "l3_episodes": [],
            "l4_acquaintance": {},
            "l4_group_slang": [],
            "l5_self_state": {"energy_level": 80, "mood": "relaxed", "current_activity": "摸鱼"},
        },
        envelope=MessageEnvelope(message_id="m1", scene="group", group_id=1, sender_qq=2, raw_text="哈哈", self_qq=888888),
        decision=GatekeeperDecision(action="direct_pass", target="bot", score=1.0, reason="r"),
        sticker_tags_hint=top_tags,
    )
    assert "[斗图配图协议]" in msgs[0]["content"]
    assert "[表情:" in msgs[0]["content"]


@pytest.mark.asyncio
async def test_v192_active_attention_mentioning_other_member_disambiguates_via_gatekeeper():
    """During an active bot chat, referencing @other_user ('给你介绍一下蓝豆是@某人') goes to Gatekeeper LLM."""
    store = StateStore(force_memory=True)
    await store.connect()
    gk = GatekeeperService(store=store)

    # Turn 1: User talks to bot -> opens active attention window
    d1 = await gk.evaluate(
        MessageEnvelope(
            message_id="att_m1",
            scene="group",
            group_id=9500,
            sender_qq=8001,
            sender_nickname="灵弈",
            raw_text="小呆呆你感觉现在怎么样",
            self_qq=888888,
        ),
        bot_name="小呆呆",
        aliases=["呆呆"],
    )
    assert d1.action == "direct_pass"

    # Turn 2: User introduces another member using @other_user while still talking to bot
    async def _llm_says_bot(env, default_target):
        return {"target": "bot", "score": 0.90, "reason": "Continuing chat with bot while introducing another member"}

    d2 = await gk.evaluate(
        MessageEnvelope(
            message_id="att_m2",
            scene="group",
            group_id=9500,
            sender_qq=8001,
            sender_nickname="灵弈",
            raw_text="给你介绍一下蓝豆是 这个人",
            mentioned_qqs=[99999],
            self_qq=888888,
        ),
        bot_name="小呆呆",
        aliases=["呆呆"],
        llm_scorer=_llm_says_bot,
    )
    assert d2.action == "soft_entry"
    assert d2.target == "bot"

    # Turn 3: If user genuinely switches to addressing @other_user, Gatekeeper LLM returns other_user -> silent + clears attention
    async def _llm_says_other(env, default_target):
        return {"target": "other_user", "score": 0.10, "reason": "Addressing another member directly"}

    d3 = await gk.evaluate(
        MessageEnvelope(
            message_id="att_m3",
            scene="group",
            group_id=9500,
            sender_qq=8001,
            sender_nickname="灵弈",
            raw_text="走开黑去",
            mentioned_qqs=[99999],
            self_qq=888888,
        ),
        bot_name="小呆呆",
        aliases=["呆呆"],
        llm_scorer=_llm_says_other,
    )
    assert d3.action == "silent"
    assert d3.target == "other_user"
    assert await store.get_user_attention_age_seconds(9500, 8001) is None

