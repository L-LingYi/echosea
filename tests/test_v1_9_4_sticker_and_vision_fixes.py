"""Regression tests for EchoSea v1.9.4 sticker throttling, auto-steal resilience, and reasoning/vision fixes."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app.core.alert_service import AlertService
from app.core.brain_service import CompanionPipeline
from app.core.event_bus import PluginEventBus
from app.core.gatekeeper import GatekeeperService
from app.core.identity_service import IdentityService
from app.core.memory_service import MemoryService
from app.core.model_hub import ModelHubService
from app.core.multimodal_service import MultimodalService
from app.core.redis_store import StateStore
from app.core.snowluma_adapter import SnowLumaAdapter
from app.core.sticker_service import StickerService
from app.models.db import ImageVisionCache, StickerAsset
from app.models.schemas import MessageEnvelope
from app.plugins.sdk import _is_sticker_candidate


def _make_png_bytes(color: tuple[int, int, int] = (20, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (24, 24), color).save(buf, format="PNG")
    return buf.getvalue()


def _make_gif_bytes() -> bytes:
    buf = io.BytesIO()
    f1 = Image.new("RGB", (16, 16), (255, 0, 0))
    f2 = Image.new("RGB", (16, 16), (0, 255, 0))
    f1.save(buf, format="GIF", save_all=True, append_images=[f2], duration=100, loop=0)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_v194_sticker_throttling_cooldown_and_reply_gap(db_session):
    """Stickers are throttled by cooldown_seconds and min_reply_gap, then allowed again once satisfied."""
    store = StateStore(force_memory=True)
    await store.connect()
    hub = ModelHubService("k-stk-throt", AlertService("k-stk-throt"))

    async def _fake_chat(provider, api_key, messages, temperature, max_tokens):
        return {"choices": [{"message": {"content": "笑死确实 [表情:大笑]"}}]}

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

    stk = StickerService()
    await stk.import_manual(
        db_session,
        [{"source_url": "https://img.example.com/laugh.png", "emotion_tags": ["大笑"]}],
    )
    await db_session.commit()

    adapter = SnowLumaAdapter(http_url="http://mock-snowluma", http_access_token="tok")
    sent_actions: list[dict] = []
    seq = 0

    async def _fake_send_http(action, params):
        nonlocal seq
        seq += 1
        sent_actions.append({"action": action, "params": params})
        return str(seq)

    adapter._send_http = _fake_send_http  # type: ignore[method-assign]

    pipe = CompanionPipeline(
        store=store,
        identity_service=IdentityService(),
        gatekeeper=GatekeeperService(store=store),
        memory_service=MemoryService(),
        model_hub=hub,
        adapter=adapter,
        event_bus=PluginEventBus(),
        sticker_service=stk,
        sticker_cooldown_seconds=180.0,
        sticker_min_reply_gap=2,
    )

    async def _send_direct(msg_id: str, text: str):
        env = MessageEnvelope(
            message_id=msg_id,
            scene="group",
            group_id=4861,
            sender_qq=1001,
            sender_nickname="群友",
            raw_text=text,
            mentioned_qqs=[888888],
            self_qq=888888,
        )
        res = await pipe.process_envelope(db_session, env)
        await db_session.commit()
        return res

    # Turn 1: sticker allowed
    r1 = await _send_direct("m1", "@bot 哈哈")
    assert r1["sticker"] is not None

    # Turn 2: suppressed by both cooldown and reply gap (replies_since increments -> 1)
    r2 = await _send_direct("m2", "@bot 再笑一次")
    assert r2["sticker"] is None

    # Turn 3: still suppressed (replies_since increments -> 2)
    r3 = await _send_direct("m3", "@bot 第三次")
    assert r3["sticker"] is None

    # Even with reply_gap == 2 satisfied, cooldown 180s still blocks
    r4 = await _send_direct("m4", "@bot 第四次")
    assert r4["sticker"] is None

    # Simulate cooldown elapsed while reply_gap >= 2 -> allowed again!
    state = await store.get_sticker_throttle_state("group:4861")
    await store.set_json(
        "sticker_throttle:group:4861",
        {"last_sent_ts": float(state["last_sent_ts"]) - 200.0, "replies_since": state["replies_since"]},
        ttl_seconds=86400,
    )
    r5 = await _send_direct("m5", "@bot 冷却好了吧")
    assert r5["sticker"] is not None


@pytest.mark.asyncio
async def test_v194_observe_group_image_survives_unwritable_dir_and_backfills(db_session, tmp_path, monkeypatch):
    """Filesystem OSError during sticker disk cache write must not abort DB stealing, and re-sighting backfills."""
    svc = StickerService(storage_dir=tmp_path / "stickers", auto_approve_threshold=3)
    png_bytes = _make_png_bytes()

    def _boom_write(self_path, data):
        raise PermissionError("simulated EACCES on data/stickers")

    monkeypatch.setattr(Path, "write_bytes", _boom_write)
    asset = await svc.observe_group_image(
        db_session,
        group_id=9901,
        image_bytes=png_bytes,
        source_url="https://multimedia.nt.qq.com.cn/download?rkey=old",
    )
    await db_session.commit()
    assert asset.id is not None
    assert asset.file_path == ""
    assert asset.appearance_count == 1

    # Restore write_bytes and simulate re-appearance with refreshed rkey -> backfills file_path & refreshes URL
    monkeypatch.undo()
    asset2 = await svc.observe_group_image(
        db_session,
        group_id=9901,
        image_bytes=png_bytes,
        source_url="https://multimedia.nt.qq.com.cn/download?rkey=fresh",
    )
    await db_session.commit()
    assert asset2.id == asset.id
    assert asset2.appearance_count == 2
    assert asset2.source_url == "https://multimedia.nt.qq.com.cn/download?rkey=fresh"
    assert asset2.file_path and Path(asset2.file_path).is_file()


def test_v194_sticker_candidate_filter_skips_regular_photos():
    """Auto-steal filter accepts mface, sub_type=1 stickers, and GIFs while skipping sub_type=0 photos."""
    assert _is_sticker_candidate({"type": "mface", "url": "https://img.example.com/1.png"}) is True
    assert _is_sticker_candidate(
        {"type": "image", "url": "https://qq.com/a", "sub_type": 1, "summary": "[动画表情]", "file": "a.jpg"}
    ) is True
    assert _is_sticker_candidate(
        {"type": "image", "url": "https://qq.com/b", "sub_type": 0, "summary": "[图片]", "file": "photo.jpg"}
    ) is False
    assert _is_sticker_candidate(
        {"type": "image", "url": "https://qq.com/c", "sub_type": 0, "summary": "[图片]", "file": "meme.gif"}
    ) is True
    assert _is_sticker_candidate({"type": "image", "url": "https://img.example.com/legacy.png"}) is True


@pytest.mark.asyncio
async def test_v194_disable_thinking_and_multimodal_mime_and_gif(db_session):
    """DeepSeek/Qwen disable_thinking sets both thinking & reasoning_effort; vision_caption & MIME detection work."""
    hub = ModelHubService("k-think", AlertService("k-think"))
    captured_payloads: list[dict] = []

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": "一只戴墨镜的搞笑猫咪"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 12},
            }

    class _FakeClient:
        async def post(self, url, headers=None, json=None, timeout=None):
            captured_payloads.append(json or {})
            return _FakeResp()

        async def aclose(self):
            return None

    await hub._http_client.aclose()
    hub._http_client = _FakeClient()  # type: ignore[assignment]

    await hub.upsert_provider(
        db_session,
        {
            "slot": "main_brain",
            "role": "primary",
            "name": "opencode-go/deepseek-v4.1-flash",
            "base_url": "https://opencode.ai/inference/go/openai/v1",
            "model_name": "deepseek-v4.1-flash",
            "api_key": "sk-test",
        },
    )
    await db_session.commit()

    png_bytes = _make_png_bytes()
    gif_bytes = _make_gif_bytes()
    mm = MultimodalService(image_fetcher=lambda _u: _return_async(png_bytes))

    prep_png = await mm.lookup_or_prepare(db_session, png_bytes, source_url="https://qq.com/dl?id=1", is_gif=False)
    assert prep_png["content_block"]["image_url"]["url"].startswith("data:image/png;base64,")

    prep_gif = await mm.lookup_or_prepare(db_session, gif_bytes, source_url="https://qq.com/dl?id=2", is_gif=False)
    assert prep_gif["is_gif_grid"] is True
    assert prep_gif["content_block"]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    store = StateStore(force_memory=True)
    await store.connect()
    pipe = CompanionPipeline(
        store=store,
        identity_service=IdentityService(),
        gatekeeper=GatekeeperService(store=store),
        memory_service=MemoryService(),
        model_hub=hub,
        adapter=SnowLumaAdapter(http_url=""),
        event_bus=PluginEventBus(),
        multimodal_service=mm,
    )
    count = await pipe.caption_image_items([{"type": "image", "url": "https://img.example.com/a"}], session=db_session)
    await db_session.commit()

    assert count == 1
    assert captured_payloads
    p = captured_payloads[-1]
    assert p["thinking"] == {"type": "disabled"}
    assert p["reasoning_effort"] == "none"
    assert p["max_tokens"] >= 120

    vis_row = (await db_session.execute(
        ImageVisionCache.__table__.select().where(ImageVisionCache.phash == prep_png["phash"])
    )).first()
    assert vis_row is not None


async def _return_async(val):
    return val
