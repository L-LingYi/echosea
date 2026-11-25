"""v1.3.3 Time-Decaying Per-User Attention Window (0-60s hot -> 60-240s linear decay) & Tiered Timeout Tests."""
from __future__ import annotations

import pytest

from app.core.gatekeeper import GatekeeperService
from app.models.schemas import MessageEnvelope


@pytest.mark.asyncio
async def test_time_decaying_attention_curve_and_followup_continuity(state_store):
    gk = GatekeeperService(
        store=state_store,
        soft_entry_threshold=0.78,
        attention_hot_seconds=60.0,
        attention_decay_seconds=240.0,
    )

    # Verify exact mathematical decay curve: 0-60s -> 0.35; 150s (midpoint) -> 0.565; >=240s -> 0.78
    assert gk.compute_attention_threshold(0.0) == 0.35
    assert gk.compute_attention_threshold(60.0) == 0.35
    assert gk.compute_attention_threshold(150.0) == 0.565
    assert gk.compute_attention_threshold(240.0) == 0.78
    assert gk.compute_attention_threshold(300.0) == 0.78

    # 1. User A strongly wakes up bot by name -> refreshes User A's attention window!
    d1 = await gk.evaluate(
        MessageEnvelope(
            message_id="a1", scene="group", group_id=486135657, sender_qq=2445955246,
            raw_text="小呆呆，你会说日语吗", self_qq=888,
        ),
        bot_name="小呆呆", aliases=["呆呆"],
    )
    assert d1.action == "direct_pass"

    # 2. User A follows up WITHOUT @ or bot name within 1 minute -> passes smoothly!
    d2 = await gk.evaluate(
        MessageEnvelope(
            message_id="a2", scene="group", group_id=486135657, sender_qq=2445955246,
            raw_text="那来一段日语自我介绍", self_qq=888,
        ),
        bot_name="小呆呆", aliases=["呆呆"],
    )
    assert d2.action == "soft_entry"
    assert "attention=" in d2.reason

    # 3. User B (different user in the same group) sends casual chat -> NOT affected by User A's attention window!
    d_other = await gk.evaluate(
        MessageEnvelope(
            message_id="b1", scene="group", group_id=486135657, sender_qq=1283439139,
            raw_text="我回去重新配置一下", self_qq=888,
        ),
        bot_name="小呆呆", aliases=["呆呆"],
    )
    assert d_other.action == "silent"

    # 4. User A says goodbye ("好了没事了") -> immediately clears attention window!
    d_bye = await gk.evaluate(
        MessageEnvelope(
            message_id="a3", scene="group", group_id=486135657, sender_qq=2445955246,
            raw_text="好了没事了", self_qq=888,
        ),
        bot_name="小呆呆", aliases=["呆呆"],
    )
    assert d_bye.action == "silent"
    assert await state_store.get_user_attention_age_seconds(486135657, 2445955246) is None
