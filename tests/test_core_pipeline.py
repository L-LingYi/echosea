"""Core unit & integration tests: QQ Identity, SnowLuma Adapter, 3-Layer Gatekeeper, 5-Layer Memory & Semi-Isolation, Anti-AI."""
from __future__ import annotations

import pytest

from app.core.brain_service import sanitize_anti_ai_flavor, split_natural_bubbles
from app.core.gatekeeper import GatekeeperService
from app.core.identity_service import IdentityService
from app.core.memory_service import MemoryService
from app.core.snowluma_adapter import SnowLumaAdapter
from app.models.db import GroupExpression, MemoryEpisode, QQIdentity
from app.models.schemas import GatekeeperDecision, MessageEnvelope


@pytest.mark.asyncio
async def test_qq_identity_strong_binding_and_known_names(db_session):
    svc = IdentityService()
    u1 = await svc.upsert_from_message(db_session, qq_id=10086, nickname="阿明", group_id=5001, group_card="卷王明")
    assert u1.qq_id == 10086
    assert "阿明" in u1.known_names and "卷王明" in u1.known_names
    assert u1.group_cards["5001"] == "卷王明"

    # User changes group card later -> QQ primary key stays bound, old name preserved in known_names
    u2 = await svc.upsert_from_message(db_session, qq_id=10086, nickname="阿明", group_id=5001, group_card="摸鱼明")
    assert u2.group_cards["5001"] == "摸鱼明"
    assert "卷王明" in u2.known_names
    assert "摸鱼明" in u2.known_names
    assert await svc.resolve_display_name(db_session, 10086, group_id=5001) == "摸鱼明"


@pytest.mark.asyncio
async def test_snowluma_normalization_and_quote_expansion(db_session):
    adapter = SnowLumaAdapter(bot_qq=888888)
    id_svc = IdentityService()
    mem_svc = MemoryService()

    # Seed original message
    await id_svc.upsert_from_message(db_session, qq_id=10001, nickname="张三", group_id=7001, group_card="三哥")
    orig_env = MessageEnvelope(
        message_id="msg_100",
        scene="group",
        group_id=7001,
        sender_qq=10001,
        sender_card="三哥",
        raw_text="今晚去吃火锅吗？",
        self_qq=888888,
    )
    await mem_svc.record_message(
        db_session,
        orig_env,
        GatekeeperDecision(action="silent", target="group_open", score=0.2, reason="test"),
    )

    # Incoming SnowLuma OneBot v11 array-segment event quoting msg_100 and @bot
    raw_event = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 888888,
        "group_id": 7001,
        "user_id": 10002,
        "message_id": "msg_101",
        "sender": {"user_id": 10002, "nickname": "李四", "card": "老李"},
        "message": [
            {"type": "reply", "data": {"id": "msg_100"}},
            {"type": "at", "data": {"qq": "888888"}},
            {"type": "text", "data": {"text": " 你觉得呢"}},
        ],
    }
    env = await adapter.normalize_event(raw_event, session=db_session)
    assert env is not None
    assert env.reply_to_msg_id == "msg_100"
    assert env.expanded_reply_text == "三哥: 今晚去吃火锅吗？"
    assert 888888 in env.mentioned_qqs
    assert env.raw_text == "你觉得呢"


@pytest.mark.asyncio
async def test_three_layer_gatekeeper_rules(state_store):
    gk = GatekeeperService(store=state_store, soft_entry_threshold=0.78, max_consecutive_turns=2)
    aliases = ["呆呆", "小呆"]

    # 1. Hard rule: @other_user -> silent + tracks active peer thread
    d1 = await gk.evaluate(
        MessageEnvelope(
            message_id="m1", scene="group", group_id=9001, sender_qq=101,
            raw_text="帮我带杯咖啡", mentioned_qqs=[102], self_qq=888,
        ),
        bot_name="小呆呆", aliases=aliases,
    )
    assert d1.action == "silent"
    assert d1.target == "other_user"
    assert d1.should_record_ambient is True

    # 2. Active peer thread guard: user 101 follows up without @ -> still silent
    d2 = await gk.evaluate(
        MessageEnvelope(
            message_id="m2", scene="group", group_id=9001, sender_qq=101,
            raw_text="要冰美式谢谢", mentioned_qqs=[], self_qq=888,
        ),
        bot_name="小呆呆", aliases=aliases,
    )
    assert d2.action == "silent"
    assert "Active peer-to-peer" in d2.reason

    # 3. Direct alias call WITHOUT @ -> direct_pass
    d3 = await gk.evaluate(
        MessageEnvelope(
            message_id="m3", scene="group", group_id=9001, sender_qq=103,
            raw_text="呆呆，今晚玩什么游戏？", mentioned_qqs=[], self_qq=888,
        ),
        bot_name="小呆呆", aliases=aliases,
    )
    assert d3.action == "direct_pass"
    assert d3.target == "bot"

    # 4. Third-person banter ABOUT bot -> downweighted soft-entry passes when cue hits threshold
    d4 = await gk.evaluate(
        MessageEnvelope(
            message_id="m4", scene="group", group_id=9001, sender_qq=104,
            raw_text="小呆呆刚才笑死我了哈哈", mentioned_qqs=[], self_qq=888,
        ),
        bot_name="小呆呆", aliases=aliases,
    )
    assert d4.action == "soft_entry"
    assert d4.target == "third_person_about_bot"

    # 5. Air guardrail: after max_consecutive_turns (2), next soft-entry is blocked
    await gk.evaluate(
        MessageEnvelope(
            message_id="m5", scene="group", group_id=9001, sender_qq=105,
            raw_text="小呆呆刚才真的离谱笑死", mentioned_qqs=[], self_qq=888,
        ),
        bot_name="小呆呆", aliases=aliases,
    )
    d6 = await gk.evaluate(
        MessageEnvelope(
            message_id="m6", scene="group", group_id=9001, sender_qq=106,
            raw_text="小呆呆刚才笑死哈哈", mentioned_qqs=[], self_qq=888,
        ),
        bot_name="小呆呆", aliases=aliases,
    )
    assert d6.action == "silent"
    assert "Air guardrail" in d6.reason

    # 6. Instant recall interception
    await state_store.mark_recalled("m_recalled")
    d7 = await gk.evaluate(
        MessageEnvelope(
            message_id="m_recalled", scene="group", group_id=9001, sender_qq=103,
            raw_text="小呆呆救命", mentioned_qqs=[888], self_qq=888,
        ),
        bot_name="小呆呆", aliases=aliases,
    )
    assert d7.action == "blocked_recall"


@pytest.mark.asyncio
async def test_five_layer_memory_semi_isolation_and_forget(db_session):
    id_svc = IdentityService()
    mem_svc = MemoryService(dm_bridge_window_minutes=30)

    identity = await id_svc.upsert_from_message(db_session, qq_id=20001, nickname="小红", group_id=3001, group_card="红姐")
    identity.public_profile = {"hobby": "原神"}
    identity.private_profile = {"secret_worry": "最近想辞职换工作"}
    await db_session.flush()

    # Add a public group episode and a private DM episode
    db_session.add(MemoryEpisode(scope="public_group", group_id=3001, qq_id=20001, title="一起开黑", content="昨晚五排连胜"))
    db_session.add(MemoryEpisode(scope="private_dm", group_id=None, qq_id=20001, title="私聊吐露心声", content="偷偷说了想跳槽"))
    db_session.add(GroupExpression(group_id=3001, phrase="寄了", meaning="事情办砸了", status="approved"))
    db_session.add(GroupExpression(group_id=3001, phrase="未审黑话", meaning="待审核", status="pending"))

    # Record a silent group message -> feeds L2 Ambient Memory
    silent_env = MessageEnvelope(
        message_id="g_silent_1", scene="group", group_id=3001, sender_qq=20001,
        sender_card="红姐", raw_text="刚才大家在讨论周末去露营", self_qq=888,
    )
    await mem_svc.record_message(
        db_session, silent_env, GatekeeperDecision(action="silent", target="group_open", score=0.2, reason="ambient")
    )
    summary = await mem_svc.build_ambient_summary(db_session, group_id=3001)
    assert summary is not None
    assert "露营" in summary.summary_text

    # 1. GROUP context MUST enforce semi-isolation: NO private_profile, NO private_dm episodes, ONLY approved slang
    group_ctx = await mem_svc.assemble_context(db_session, scene="group", sender_qq=20001, group_id=3001)
    assert "private_profile" not in group_ctx["l4_acquaintance"]
    assert group_ctx["l4_acquaintance"]["public_profile"] == {"hobby": "原神"}
    assert all(ep["scope"] == "public_group" for ep in group_ctx["l3_episodes"])
    assert len(group_ctx["l2_ambient_summaries"]) == 1
    assert [s["phrase"] for s in group_ctx["l4_group_slang"]] == ["寄了"]

    # 2. PRIVATE DM context CAN see private_profile, private_dm episodes, AND 30m Group->DM bridge
    dm_ctx = await mem_svc.assemble_context(db_session, scene="private", sender_qq=20001)
    assert dm_ctx["l4_acquaintance"]["private_profile"] == {"secret_worry": "最近想辞职换工作"}
    assert any(ep["scope"] == "private_dm" for ep in dm_ctx["l3_episodes"])
    assert len(dm_ctx["l1_group_to_dm_bridge"]) == 1
    assert "露营" in dm_ctx["l1_group_to_dm_bridge"][0]["text"]

    # 3. One-Click Forget cascades across messages, episodes, ambient summaries, and profiles
    stats = await mem_svc.forget_user_data(db_session, qq_id=20001, wipe_profile=True)
    assert stats["messages_deleted"] == 1
    assert stats["episodes_deleted"] == 2
    assert stats["ambient_summaries_deleted"] == 1
    assert stats["profiles_wiped"] == 1
    refreshed = await db_session.get(QQIdentity, 20001)
    assert refreshed.public_profile == {}
    assert refreshed.private_profile == {}


def test_anti_ai_flavor_and_quote_preserving_split():
    raw = "**好的**，接住了！作为AI，我理解你的感受。刚才他说“今天绝对不加班！结果九点还在改Bug。”笑死我了！快去吃饭吧。"
    cleaned = sanitize_anti_ai_flavor(raw)
    assert "接住了" not in cleaned
    assert "作为AI" not in cleaned
    assert "我理解你的感受" not in cleaned
    assert "**" not in cleaned

    bubbles = split_natural_bubbles(cleaned, max_bubbles=3)
    assert 1 <= len(bubbles) <= 3
    # Verify the quoted span was NEVER broken in half across bubbles
    assert any("“今天绝对不加班！结果九点还在改Bug。”" in b for b in bubbles)
