"""Main Brain Service (v1.8):
- Unified `request_id` privacy-safe pipeline tracing (inbound -> gatekeeper -> main_brain -> tts -> onebot).
- Wires `gatekeeper` model slot (native TypeSafe AI Jev System One or OpenAI-compatible fallback).
- Per-group async lock serialization + optional WS burst debouncing + Multimodal vision cache.
- Delegates prompt construction to `prompt_builder` and web search sessions to `search_orchestrator`.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func as _sql_func
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.event_bus import PluginEventBus
from app.core.gatekeeper import GatekeeperService
from app.core.identity_service import IdentityService
from app.core.memory_service import MemoryService
from app.core.model_hub import ModelHubService
from app.core.multimodal_service import MultimodalService, resolve_image_phash
from app.core.prompt_builder import (
    DEFAULT_BANNED_PHRASES,
    DEFAULT_SYSTEM_PROMPT,
    build_prompt_messages,
    format_casual_bubbles,
    sanitize_anti_ai_flavor,
    split_natural_bubbles,
)
from app.core.redis_store import MessageDebouncer, StateStore
from app.core.search_orchestrator import WEB_SEARCH_TOOLS, SearchOrchestrator
from app.core.snowluma_adapter import SnowLumaAdapter
from app.core.sticker_service import StickerService, extract_sticker_tags
from app.core.tts_service import DoubaoTTSService, detect_voice_intent
from app.core.web_perception_service import WebPerceptionService
from app.models.db import (
    ChatMessage,
    EvolutionSuggestion,
    ImageVisionCache,
    OutboxDelivery,
    PersonaVersion,
    PipelineTrace,
    QQIdentity,
    StickerAsset,
    SystemConfig,
)
from app.models.schemas import GatekeeperDecision, MessageEnvelope

REJECTION_FEEDBACK_CUES = (
    "闭嘴",
    "别说话",
    "别回了",
    "少插嘴",
    "别接话",
    "没问你",
    "不是问你",
    "人工智障",
    "答非所问",
    "烦死了",
    "真蠢",
    "智障",
)
ENGAGEMENT_FEEDBACK_CUES = (
    "哈哈",
    "笑死",
    "草",
    "确实",
    "对对",
    "牛",
    "666",
    "好家伙",
    "有道理",
    "懂了",
    "谢谢",
    "谢啦",
    "不错",
    "可以",
    "真的假的",
    "细说",
    "乐",
    "典",
    "绷不住",
)

logger = logging.getLogger(__name__)

__all__ = [
    "CompanionPipeline",
    "DEFAULT_BANNED_PHRASES",
    "DEFAULT_SYSTEM_PROMPT",
    "WEB_SEARCH_TOOLS",
    "format_casual_bubbles",
    "sanitize_anti_ai_flavor",
    "split_natural_bubbles",
]


class CompanionPipeline:
    """End-to-end orchestrator across all subsystems with unified request_id tracing."""

    def __init__(
        self,
        store: StateStore,
        identity_service: IdentityService,
        gatekeeper: GatekeeperService,
        memory_service: MemoryService,
        model_hub: ModelHubService,
        adapter: SnowLumaAdapter,
        event_bus: PluginEventBus,
        sticker_service: StickerService | None = None,
        tts_service: DoubaoTTSService | None = None,
        web_perception: WebPerceptionService | None = None,
        multimodal_service: MultimodalService | None = None,
        admin_qq: int = 0,
        group_rate_limit_per_min: int = 12,
        global_rate_limit_per_min: int = 40,
        ambient_summary_trigger_count: int = 20,
        ideal_gatekeeper_ms: int = 5000,
        ideal_reply_ms: int = 15000,
        gatekeeper_fallback_timeout_seconds: float = 18.0,
        stale_soft_entry_seconds: float = 35.0,
        stale_soft_entry_min_msgs: int = 5,
        smart_quote_min_intervening: int = 3,
        smart_quote_min_delay_seconds: float = 25.0,
        debounce_window_seconds: float = 0.0,
        debounce_max_wait_seconds: float = 6.0,
        bubble_delay_min_seconds: float = 0.0,
        bubble_delay_max_seconds: float = 0.0,
        enable_background_image_caption: bool = True,
        session_factory: Any = None,
    ) -> None:
        self.store = store
        self.identity_service = identity_service
        self.gatekeeper = gatekeeper
        self.memory_service = memory_service
        self.model_hub = model_hub
        self.adapter = adapter
        self.event_bus = event_bus
        self.sticker_service = sticker_service
        self.tts_service = tts_service
        self.web_perception = web_perception
        self.multimodal_service = multimodal_service
        self.search_orchestrator = SearchOrchestrator(model_hub=model_hub, web_perception=web_perception)
        self.admin_qq = admin_qq
        self.group_rate_limit_per_min = group_rate_limit_per_min
        self.global_rate_limit_per_min = global_rate_limit_per_min
        self.ambient_summary_trigger_count = ambient_summary_trigger_count
        self.ideal_gatekeeper_ms = ideal_gatekeeper_ms
        self.ideal_reply_ms = ideal_reply_ms
        self.gatekeeper_fallback_timeout_seconds = gatekeeper_fallback_timeout_seconds
        self.stale_soft_entry_seconds = stale_soft_entry_seconds
        self.stale_soft_entry_min_msgs = stale_soft_entry_min_msgs
        self.smart_quote_min_intervening = smart_quote_min_intervening
        self.smart_quote_min_delay_seconds = smart_quote_min_delay_seconds
        self._debounce_window_seconds = max(0.0, float(debounce_window_seconds))
        self.debouncer = MessageDebouncer(
            window_seconds=self._debounce_window_seconds or 2.2,
            max_wait_seconds=debounce_max_wait_seconds,
        )
        self.bubble_delay_min_seconds = max(0.0, float(bubble_delay_min_seconds))
        self.bubble_delay_max_seconds = max(self.bubble_delay_min_seconds, float(bubble_delay_max_seconds))
        self.enable_background_image_caption = bool(enable_background_image_caption)
        self._session_factory = session_factory
        self._group_msg_counters: dict[int, int] = {}
        self._group_locks: dict[int, asyncio.Lock] = {}
        self._group_generations: dict[int, int] = {}
        self._bg_caption_sem = asyncio.Semaphore(1)
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    build_prompt_messages = staticmethod(build_prompt_messages)

    def _get_group_lock(self, group_id: int) -> asyncio.Lock:
        lock = self._group_locks.get(group_id)
        if lock is None:
            lock = asyncio.Lock()
            self._group_locks[group_id] = lock
        return lock

    def _next_group_generation(self, group_id: int) -> int:
        gen = self._group_generations.get(group_id, 0) + 1
        self._group_generations[group_id] = gen
        return gen

    def _compute_bubble_delay(self, bubble_text: str) -> float:
        if self.bubble_delay_max_seconds <= 0.0:
            return 0.0
        base = 0.55 + len(bubble_text.strip()) * 0.04 + random.uniform(0.0, 0.25)
        return max(self.bubble_delay_min_seconds, min(self.bubble_delay_max_seconds, base))

    async def drain_background_tasks(self, timeout: float = 3.0) -> None:
        pending = [t for t in self._bg_tasks if not t.done()]
        if pending:
            _, still = await asyncio.wait(pending, timeout=max(0.1, timeout))
            for t in still:
                t.cancel()
            if still:
                await asyncio.gather(*still, return_exceptions=True)

    @staticmethod
    async def _trace(
        session: AsyncSession,
        request_id: str,
        scene: str,
        group_id: int | None,
        stage: str,
        status: str,
        latency_ms: int = 0,
        error_type: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Record privacy-safe stage telemetry (never logs raw chat text)."""
        session.add(
            PipelineTrace(
                request_id=request_id,
                scene=scene,
                group_id=group_id,
                stage=stage,
                status=status,
                latency_ms=latency_ms,
                error_type=error_type,
                meta_json=meta or {},
            )
        )
        logger.info(
            "trace request_id=%s scene=%s group_id=%s stage=%s status=%s latency_ms=%d error=%s",
            request_id,
            scene,
            group_id,
            stage,
            status,
            latency_ms,
            error_type or "none",
        )

    async def get_active_persona(self, session: AsyncSession) -> PersonaVersion:
        stmt = select(PersonaVersion).where(PersonaVersion.is_active.is_(True)).order_by(PersonaVersion.id.desc()).limit(1)
        persona = (await session.execute(stmt)).scalar_one_or_none()
        if persona is None:
            persona = PersonaVersion(
                version_tag="v1.0-default",
                bot_name="小呆呆",
                aliases=["呆呆", "小呆"],
                system_prompt=DEFAULT_SYSTEM_PROMPT,
                few_shot_examples=[
                    {"user": "今天累死了", "assistant": "摸摸，又被老板抓去当黑奴了？早点躺平算了"},
                    {"user": "小呆呆刚才在聊啥", "assistant": "刚才那俩人一直在争晚饭吃麻辣烫还是烧烤，笑死"},
                ],
                banned_phrases=list(DEFAULT_BANNED_PHRASES),
                is_active=True,
            )
            session.add(persona)
            await session.flush()
        return persona

    async def resolve_effective_config(
        self,
        session: AsyncSession,
        group_id: int | None = None,
        user_qq: int | None = None,
    ) -> dict[str, Any]:
        """3-layer config inheritance: global -> group -> user."""
        merged: dict[str, Any] = {
            "enabled": True,
            "soft_entry_threshold": self.gatekeeper.soft_entry_threshold,
            "max_bubbles": 3,
        }
        scopes = [("global", "default")]
        if group_id is not None:
            scopes.append(("group", str(group_id)))
        if user_qq is not None:
            scopes.append(("user", str(user_qq)))

        for scope_type, scope_id in scopes:
            stmt = select(SystemConfig).where(
                SystemConfig.scope_type == scope_type,
                SystemConfig.scope_id == scope_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row and isinstance(row.config_json, dict):
                merged.update(row.config_json)
        return merged

    @staticmethod
    def _merge_burst_envelopes(batch: list[MessageEnvelope]) -> MessageEnvelope:
        """Combine rapid consecutive envelopes from the same speaker into one envelope."""
        if len(batch) == 1:
            return batch[0]
        latest = batch[-1]
        texts = [e.raw_text.strip() for e in batch if (e.raw_text or "").strip()]
        merged_mentions: list[int] = []
        merged_media: list[dict[str, Any]] = []
        for env in batch:
            for qq in env.mentioned_qqs:
                if qq not in merged_mentions:
                    merged_mentions.append(qq)
            merged_media.extend(env.media_meta)
        return latest.model_copy(
            update={
                "raw_text": "\n".join(texts),
                "mentioned_qqs": merged_mentions,
                "media_meta": merged_media,
            }
        )

    async def handle_raw_onebot_event(self, session: AsyncSession, raw_event: dict[str, Any]) -> dict[str, Any]:
        """Entry point for synchronous/HTTP OneBot v11 events (message or notice)."""
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        post_type = raw_event.get("post_type")

        if post_type == "notice" and raw_event.get("notice_type") in ("group_recall", "friend_recall"):
            msg_id = str(raw_event.get("message_id", ""))
            if msg_id:
                self.debouncer.remove_recalled_message(msg_id)
                await self.store.mark_recalled(msg_id)
                updated = await self.memory_service.mark_message_recalled(session, msg_id)
                await self._trace(
                    session, request_id, "group" if raw_event.get("group_id") else "private",
                    raw_event.get("group_id"), "recall", "ok", meta={"updated_rows": updated},
                )
                await self.event_bus.emit("on_message_recalled", {"message_id": msg_id, "request_id": request_id})
                return {"handled": "recall", "request_id": request_id, "message_id": msg_id, "updated_rows": updated}
            return {"handled": "recall_ignored", "request_id": request_id}

        t_in = time.perf_counter()
        envelope = await self.adapter.normalize_event(raw_event, session=session)
        if envelope is None:
            return {"handled": "ignored", "request_id": request_id}

        await self._trace(
            session, request_id, envelope.scene, envelope.group_id, "inbound", "ok",
            latency_ms=int((time.perf_counter() - t_in) * 1000),
            meta={"has_reply": bool(envelope.reply_to_msg_id), "media_count": len(envelope.media_meta)},
        )
        return await self.process_envelope(session, envelope, request_id=request_id)

    async def enqueue_or_handle_raw_event(self, session_factory: Any, raw_event: dict[str, Any]) -> dict[str, Any]:
        """WebSocket ingress: handles recalls immediately and debounces non-direct group burst chatter."""
        if self._session_factory is None:
            self._session_factory = session_factory
        post_type = raw_event.get("post_type")
        if post_type == "notice" or self._debounce_window_seconds <= 0.0:
            async with session_factory() as s:
                res = await self.handle_raw_onebot_event(s, raw_event)
                await s.commit()
                return res

        async with session_factory() as s:
            envelope = await self.adapter.normalize_event(raw_event, session=s)
            await s.commit()
        if envelope is None:
            return {"handled": "ignored"}

        dkey = f"{envelope.scene}:{envelope.group_id or 0}:{envelope.sender_qq}"
        is_immediate = (
            envelope.scene == "private"
            or (envelope.self_qq > 0 and envelope.self_qq in envelope.mentioned_qqs)
            or bool(envelope.reply_to_msg_id)
        )

        async def _run_batch(batch: list[MessageEnvelope]) -> dict[str, Any]:
            merged = self._merge_burst_envelopes(batch)
            req_id = f"req_{uuid.uuid4().hex[:12]}"
            async with session_factory() as s:
                await self._trace(
                    s, req_id, merged.scene, merged.group_id, "inbound", "ok",
                    meta={"batched_count": len(batch), "media_count": len(merged.media_meta)},
                )
                out = await self.process_envelope(s, merged, request_id=req_id)
                await s.commit()
                return out

        if is_immediate:
            pending = await self.debouncer.flush_key_now(dkey, None)
            return await _run_batch([*pending, envelope])

        await self.debouncer.push(dkey, envelope, _run_batch)
        return {"handled": "debounced", "debounce_key": dkey}

    async def _collect_vision_context(
        self,
        session: AsyncSession,
        envelope: MessageEnvelope,
        decision: GatekeeperDecision,
    ) -> tuple[list[dict[str, Any]], list[str], list[str], int]:
        """On-demand vision: decode/cache up to 2 images on direct_pass, quoted image, high-heat sticker, or cache hit."""
        if self.multimodal_service is None:
            return [], [], [], 0

        candidate_items: list[tuple[dict[str, Any], bool]] = [
            (m, False) for m in (envelope.media_meta or []) if m.get("type") in ("image", "mface") and m.get("url")
        ]
        # Include quoted message's image if user replied to an image message
        if envelope.reply_to_msg_id and len(candidate_items) < 2:
            q_stmt = (
                select(ChatMessage.media_meta)
                .where(
                    ChatMessage.message_id == envelope.reply_to_msg_id,
                    ChatMessage.scene == envelope.scene,
                    ChatMessage.is_recalled.is_(False),
                )
                .limit(1)
            )
            q_media = (await session.execute(q_stmt)).scalar_one_or_none() or []
            for m in q_media:
                if isinstance(m, dict) and m.get("type") in ("image", "mface") and m.get("url"):
                    candidate_items.append((m, True))

        candidate_items = candidate_items[:2]
        if not candidate_items:
            return [], [], [], 0

        image_blocks: list[dict[str, Any]] = []
        vision_hints: list[str] = []
        pending_phashes: list[str] = []
        cache_hits = 0
        fetcher = getattr(self.multimodal_service, "image_fetcher", None)

        for item, is_quoted in candidate_items:
            url = str(item["url"])
            is_gif = url.lower().endswith(".gif")
            # Check URL match in vision cache first to avoid unnecessary downloads
            cached_by_url = (
                await session.execute(select(ImageVisionCache).where(ImageVisionCache.source_url == url).limit(1))
            ).scalar_one_or_none()
            if cached_by_url is not None and cached_by_url.description:
                vision_hints.append(cached_by_url.description)
                cache_hits += 1
                continue

            allow_decode = decision.action == "direct_pass" or (is_quoted and decision.action == "soft_entry")
            if not allow_decode and decision.action == "soft_entry":
                hot_sticker = (
                    await session.execute(
                        select(StickerAsset)
                        .where(
                            StickerAsset.source_url == url,
                            or_(StickerAsset.appearance_count >= 2, StickerAsset.positive_reactions >= 1),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if hot_sticker is not None or (cached_by_url is not None and cached_by_url.hit_count >= 2):
                    allow_decode = True

            if not allow_decode:
                continue

            phash, img_bytes = await resolve_image_phash(url, timeout=3.0, fetcher=fetcher)
            if img_bytes is not None:
                res = await self.multimodal_service.lookup_or_prepare(session, img_bytes, source_url=url, is_gif=is_gif)
                if res.get("cache_hit") and res.get("description"):
                    vision_hints.append(str(res["description"]))
                    cache_hits += 1
                elif res.get("content_block"):
                    image_blocks.append(res["content_block"])
                    pending_phashes.append(str(res["phash"]))
            else:
                cached_by_hash = (
                    await session.execute(select(ImageVisionCache).where(ImageVisionCache.phash == phash).limit(1))
                ).scalar_one_or_none()
                if cached_by_hash is not None and cached_by_hash.description:
                    vision_hints.append(cached_by_hash.description)
                    cache_hits += 1

        return image_blocks, vision_hints, pending_phashes, cache_hits

    async def _evaluate_inbound_social_feedback(
        self,
        session: AsyncSession,
        envelope: MessageEnvelope,
        persona: PersonaVersion,
        replied_to_bot: bool,
    ) -> str | None:
        """Backfill implicit social feedback (engaged / rejected) on bot group replies within 120s."""
        if envelope.scene != "group" or envelope.group_id is None:
            return None
        bot_qq = envelope.self_qq or self.adapter.bot_qq
        if bot_qq and envelope.sender_qq == bot_qq:
            return None

        now_utc = datetime.now(timezone.utc)
        cutoff = now_utc - timedelta(seconds=120)
        pending_rows = list(
            (
                await session.execute(
                    select(OutboxDelivery)
                    .where(
                        OutboxDelivery.scene == "group",
                        OutboxDelivery.target_id == envelope.group_id,
                        OutboxDelivery.status == "sent",
                        OutboxDelivery.feedback_status == "pending",
                        OutboxDelivery.created_at >= cutoff,
                    )
                    .order_by(OutboxDelivery.created_at.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        if not pending_rows:
            return None

        latest = pending_rows[0]
        text = (envelope.raw_text or "").strip()
        if not text:
            return None

        bot_mentioned = bool(bot_qq and bot_qq in envelope.mentioned_qqs)
        other_mentioned = any(q != bot_qq for q in envelope.mentioned_qqs)
        all_names = [n.strip().lower() for n in [persona.bot_name, *(persona.aliases or [])] if n and n.strip()]
        mentions_bot_name = any(name in text.lower() for name in all_names)

        verdict: str | None = None
        if any(cue in text for cue in REJECTION_FEEDBACK_CUES) and (replied_to_bot or bot_mentioned or mentions_bot_name or not other_mentioned):
            verdict = "rejected"
        elif latest.trigger_msg_id:
            orig_msg = (
                await session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.message_id == latest.trigger_msg_id, ChatMessage.group_id == envelope.group_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if orig_msg is not None and orig_msg.sender_qq == envelope.sender_qq and not replied_to_bot:
                q1 = self.memory_service._tokenize_for_recall(orig_msg.raw_text)
                q2 = self.memory_service._tokenize_for_recall(text)
                if q1 and q2 and (len(q1 & q2) / len(q1 | q2)) >= 0.75:
                    verdict = "rejected"

        if verdict is None and not other_mentioned:
            if replied_to_bot or bot_mentioned or mentions_bot_name or any(cue in text for cue in ENGAGEMENT_FEEDBACK_CUES):
                verdict = "engaged"

        if verdict is None:
            return None

        target_req_id = latest.request_id
        for row in pending_rows:
            if row.request_id == target_req_id or target_req_id is None:
                row.feedback_status = verdict
                row.feedback_updated_at = now_utc

        ident = await session.get(QQIdentity, envelope.sender_qq)
        if ident is not None:
            delta = 1 if verdict == "engaged" else -2
            ident.relationship_level = max(0, int(ident.relationship_level or 0) + delta)

        if verdict == "engaged" and target_req_id:
            trace_row = (
                await session.execute(
                    select(PipelineTrace)
                    .where(PipelineTrace.request_id == target_req_id, PipelineTrace.stage == "main_brain")
                    .order_by(PipelineTrace.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            ep_ids = (trace_row.meta_json or {}).get("recalled_episode_ids", []) if trace_row else []
            if ep_ids:
                await self.memory_service.reinforce_episodes(session, ep_ids)

        await session.flush()
        return verdict

    async def process_envelope(
        self,
        session: AsyncSession,
        envelope: MessageEnvelope,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if envelope.scene == "group" and envelope.group_id is not None:
            my_gen = self._next_group_generation(envelope.group_id)
            async with self._get_group_lock(envelope.group_id):
                res = await self._process_envelope_locked(
                    session,
                    envelope,
                    request_id=request_id,
                    defer_multi_bubble=True,
                )
            if isinstance(res, dict) and "_deferred_send" in res:
                return await self._finalize_reply_delivery(
                    session,
                    res["_deferred_send"],
                    group_id=envelope.group_id,
                    generation_id=my_gen,
                )
            return res
        return await self._process_envelope_locked(session, envelope, request_id=request_id)

    async def _process_envelope_locked(
        self,
        session: AsyncSession,
        envelope: MessageEnvelope,
        request_id: str | None = None,
        defer_multi_bubble: bool = False,
    ) -> dict[str, Any]:
        request_id = request_id or f"req_{uuid.uuid4().hex[:12]}"
        session_scope = (
            f"group:{envelope.group_id}" if envelope.group_id is not None else f"private:{envelope.sender_qq}"
        )
        t_start = time.perf_counter()
        bot_mentioned = bool(envelope.self_qq and envelope.self_qq in envelope.mentioned_qqs)
        replied_to_bot = bool(envelope.self_qq and envelope.quoted_sender_qq == envelope.self_qq)
        if not (envelope.raw_text or "").strip() and not envelope.media_meta and not bot_mentioned and not replied_to_bot:
            return {"handled": "empty_message_ignored", "request_id": request_id}

        # 1. Strong QQ Identity Binding
        await self.identity_service.upsert_from_message(
            session,
            qq_id=envelope.sender_qq,
            nickname=envelope.sender_nickname,
            group_id=envelope.group_id,
            group_card=envelope.sender_card,
        )

        # 2. Load active persona & 3-layer inherited config
        persona = await self.get_active_persona(session)
        cfg = await self.resolve_effective_config(session, group_id=envelope.group_id, user_qq=envelope.sender_qq)
        if not cfg.get("enabled", True):
            return {"handled": "disabled_by_config", "request_id": request_id}

        # 2b. Implicit Social Feedback Backfill (observe group reactions within 120s of bot reply)
        if envelope.scene == "group" and envelope.group_id is not None:
            await self._evaluate_inbound_social_feedback(session, envelope, persona, replied_to_bot)

        # 3. Three-Layer Gatekeeper Evaluation (lock-free threshold passed per request)
        t_gk = time.perf_counter()
        budget_fuse = await self.model_hub.is_budget_fuse_active(session)
        req_soft_entry_threshold = float(cfg.get("soft_entry_threshold", self.gatekeeper.soft_entry_threshold))

        async def _gatekeeper_model_scorer(env: MessageEnvelope, default_target: str) -> dict[str, Any] | None:
            recent_lines: list[str] = []
            if env.scene == "group" and env.group_id is not None:
                r_stmt = (
                    select(
                        ChatMessage.sender_display_name,
                        ChatMessage.raw_text,
                        ChatMessage.media_meta,
                        ChatMessage.gatekeeper_decision,
                    )
                    .where(
                        ChatMessage.scene == "group",
                        ChatMessage.group_id == env.group_id,
                        ChatMessage.is_recalled.is_(False),
                    )
                    .order_by(ChatMessage.id.desc())
                    .limit(5)
                )
                recent_rows = list(reversed((await session.execute(r_stmt)).all()))
                url_desc_map = await MultimodalService.resolve_media_hints_map(
                    session, [r[2] for r in recent_rows]
                )
                for sender_name, raw_txt, m_meta, gk_dec in recent_rows:
                    txt_part = (raw_txt or "").strip()
                    media_part = MultimodalService.format_media_placeholder(m_meta, url_desc_map)
                    combined = " ".join(p for p in (txt_part, media_part) if p)
                    if not combined:
                        continue
                    prefix = f"我({persona.bot_name})" if gk_dec == "bot_reply" else sender_name
                    recent_lines.append(f"{prefix}: {combined}")

            ctx_block = "\n".join(recent_lines) if recent_lines else "(none)"
            latest_media = MultimodalService.format_media_placeholder(env.media_meta)
            latest_msg_display = " ".join(p for p in ((env.raw_text or "").strip(), latest_media) if p)
            state_text = (
                f"Bot name: {persona.bot_name} (aliases: {', '.join(persona.aliases or [])})\n"
                f"Group: {env.group_id}\n"
                f"Recent group context (oldest -> newest):\n{ctx_block}\n"
                f"Latest sender: {env.display_name} (QQ:{env.sender_qq})\n"
                f"Quoted: {env.expanded_reply_text or 'none'}\n"
                f"Latest message: {latest_msg_display}"
            )
            if env.scene == "group" and env.group_id is not None:
                att_age = await self.store.get_user_attention_age_seconds(env.group_id, env.sender_qq)
                att_meta = await self.store.get_user_attention_meta(env.group_id, env.sender_qq)
                last_human = await self.store.get_last_group_human_message(env.group_id)
                interrupted = bool(
                    last_human
                    and int(last_human.get("qq", 0)) != env.sender_qq
                    and att_meta is not None
                    and (
                        int(last_human.get("seq", 0)) > int(att_meta.get("seq", 0))
                        or float(last_human.get("ts", 0.0)) > float(att_meta.get("ts", 0.0))
                    )
                )
                if interrupted:
                    state_text += (
                        "\n[Third-Party Topic Interruption] Another group member spoke or posted media after this user's last bot interaction; "
                        "verify carefully whether this message is replying to that member ('other_user') instead of the bot."
                    )
                elif att_age is not None:
                    state_text += f"\n[Active Attention Window] Bot replied to this user {int(att_age)}s ago; prefer continuing conversation unless clearly talking to someone else."
                else:
                    group_bot_age = await self.store.get_group_bot_activity_age_seconds(env.group_id)
                    if group_bot_age is not None and group_bot_age <= self.gatekeeper.group_topic_warmth_seconds:
                        state_text += (
                            f"\n[Recent Group Bot Topic] Bot replied in this group {int(group_bot_age)}s ago; "
                            "if this message riffs on or continues the bot's topic (and is not directed at another member), a brief chime-in is natural."
                        )
            return await self.model_hub.evaluate_gatekeeper_slot(
                session=session,
                state_text=state_text,
                default_target=default_target,
                request_id=request_id,
                session_scope=session_scope,
                timeout_override=self.gatekeeper_fallback_timeout_seconds,
            )

        decision = await self.gatekeeper.evaluate(
            envelope=envelope,
            bot_name=persona.bot_name,
            aliases=list(persona.aliases or []),
            replied_to_bot=replied_to_bot,
            budget_fuse_active=budget_fuse,
            admin_qq=self.admin_qq,
            llm_scorer=_gatekeeper_model_scorer,
            soft_entry_threshold=req_soft_entry_threshold,
        )
        gk_ms = int((time.perf_counter() - t_gk) * 1000)
        gk_slow = gk_ms > self.ideal_gatekeeper_ms
        await self._trace(
            session, request_id, envelope.scene, envelope.group_id, "gatekeeper", decision.action,
            latency_ms=gk_ms,
            meta={
                "target": decision.target,
                "score": decision.score,
                "slow": gk_slow,
                "reason_code": self._gatekeeper_reason_code(decision),
            },
        )
        if gk_slow:
            await self.model_hub.alert_service.notify(
                session=session,
                severity="warning",
                category="slow_response",
                title=f"门卫耗时超过理想阈值 ({gk_ms}ms > {self.ideal_gatekeeper_ms}ms)",
                detail=f"请求 {request_id} 门卫判定耗时 {gk_ms}ms，已继续正常处理未丢弃消息。",
            )

        # 4. Dual-Path Persistence: ALWAYS record message (Ambient Memory never drops silent group chat!)
        if decision.action != "blocked_recall":
            await self.memory_service.record_message(session, envelope, decision)
            if envelope.scene == "group" and envelope.group_id:
                count = self._group_msg_counters.get(envelope.group_id, 0) + 1
                self._group_msg_counters[envelope.group_id] = count
                if count >= self.ambient_summary_trigger_count:
                    self._group_msg_counters[envelope.group_id] = 0
                    await self.memory_service.build_ambient_summary(session, envelope.group_id)

            if (
                envelope.scene == "group"
                and decision.action in ("silent", "soft_entry")
                and not budget_fuse
                and self.enable_background_image_caption
                and self.multimodal_service is not None
                and self._session_factory is not None
            ):
                img_items = [
                    m for m in (envelope.media_meta or [])
                    if isinstance(m, dict) and m.get("type") in ("image", "mface") and m.get("url")
                ][:2]
                if img_items:
                    self._schedule_background_image_caption(img_items)

        self.event_bus.emit_background(
            "on_message_evaluated",
            {"request_id": request_id, "envelope": envelope.model_dump(mode="json"), "decision": decision.model_dump()},
            max_concurrency=2,
        )

        if decision.action in ("silent", "blocked_recall"):
            return {"handled": decision.action, "request_id": request_id, "decision": decision.model_dump()}

        # 5. Anti-risk rate limiting
        target_id = envelope.group_id if envelope.scene == "group" else envelope.sender_qq
        if envelope.scene == "group" and envelope.group_id:
            if not await self.store.check_rate_limit(f"group:{envelope.group_id}", self.group_rate_limit_per_min):
                return {"handled": "rate_limited_group", "request_id": request_id, "decision": decision.model_dump()}
        if not await self.store.check_rate_limit("global", self.global_rate_limit_per_min):
            return {"handled": "rate_limited_global", "request_id": request_id, "decision": decision.model_dump()}

        # 6. Doubao TTS 3-Intent Shortcut ('read_quote')
        voice_intent = detect_voice_intent(envelope.raw_text, has_reply_quote=bool(envelope.expanded_reply_text))
        quoted_text = envelope.quoted_text or envelope.expanded_reply_text
        if voice_intent == "read_quote" and quoted_text and self.tts_service is not None:
            t_tts = time.perf_counter()
            tts_res = await self.tts_service.synthesize_and_record(
                session=session,
                scene=envelope.scene,
                target_id=target_id or envelope.sender_qq,
                text=quoted_text,
                intent="read_quote",
                request_id=request_id,
            )
            await self._trace(
                session, request_id, envelope.scene, envelope.group_id, "tts",
                "synthesized" if tts_res["ok"] else "fallback",
                latency_ms=int((time.perf_counter() - t_tts) * 1000),
                error_type=None if tts_res["ok"] else "tts_fallback",
            )
            if tts_res["ok"]:
                out = await self.adapter.send_voice(session, envelope.scene, target_id or envelope.sender_qq, tts_res["audio_b64"])
                out.request_id = request_id
                await self.tts_service.mark_onebot_receipt(session, tts_res["delivery_id"], out.onebot_echo, out.onebot_status)
                await self._trace(session, request_id, envelope.scene, envelope.group_id, "onebot", out.onebot_status)
                if out.status == "sent":
                    return {"handled": "voice_read_quote", "request_id": request_id, "decision": decision.model_dump(), "tts": tts_res}
            out = await self.adapter.send_message(session, envelope.scene, target_id or envelope.sender_qq, quoted_text)
            out.request_id = request_id
            await self._trace(session, request_id, envelope.scene, envelope.group_id, "onebot", out.onebot_status)
            return {"handled": "voice_fallback_text", "request_id": request_id, "decision": decision.model_dump(), "tts": tts_res}

        # 7. Assemble 5-Layer Memory (Hybrid L3 Recall) + On-demand Multimodal Vision + Slow Daily Briefing + 48h Self-Correction Hints
        image_blocks, vision_hints, pending_vision_phashes, vision_cache_hits = await self._collect_vision_context(
            session, envelope, decision
        )
        recall_query = (
            (envelope.raw_text or "").strip()
            or (envelope.quoted_text or "").strip()
            or (envelope.expanded_reply_text or "").strip()
        )
        query_embedding = await self.model_hub.embed_text(session, recall_query)
        memory_ctx = await self.memory_service.assemble_context(
            session=session,
            scene=envelope.scene,
            sender_qq=envelope.sender_qq,
            group_id=envelope.group_id,
            query_text=recall_query,
            query_embedding=query_embedding,
        )
        briefing_text = ""
        if self.web_perception is not None:
            briefing_text = await self.web_perception.get_latest_briefing_text(session)

        now_utc = datetime.now(timezone.utc)
        hint_rows = list(
            (
                await session.execute(
                    select(EvolutionSuggestion)
                    .where(
                        EvolutionSuggestion.suggestion_type == "correction_hint",
                        EvolutionSuggestion.status == "active",
                        or_(EvolutionSuggestion.expires_at.is_(None), EvolutionSuggestion.expires_at > now_utc),
                    )
                    .order_by(EvolutionSuggestion.created_at.desc())
                    .limit(3)
                )
            )
            .scalars()
            .all()
        )
        correction_hints = [
            str((r.content_json or {}).get("hint") or r.title).strip()
            for r in hint_rows
            if str((r.content_json or {}).get("hint") or r.title).strip()
        ]

        bot_qq = envelope.self_qq or self.adapter.bot_qq
        other_mentioned_qqs = [q for q in envelope.mentioned_qqs if q != bot_qq]
        mentioned_users: list[str] = []
        if other_mentioned_qqs:
            ident_rows = {
                row.qq_id: row
                for row in (
                    await session.execute(select(QQIdentity).where(QQIdentity.qq_id.in_(other_mentioned_qqs)))
                ).scalars().all()
            }
            for q in other_mentioned_qqs:
                ident = ident_rows.get(q)
                if ident is not None:
                    cards = ident.group_cards if isinstance(ident.group_cards, dict) else {}
                    group_name = cards.get(str(envelope.group_id)) if envelope.group_id else None
                    disp = str(group_name or ident.nickname or "").strip()
                    mentioned_users.append(f"{disp}(QQ:{q})" if disp else f"QQ:{q}")
                else:
                    mentioned_users.append(f"QQ:{q}")

        messages = self.build_prompt_messages(
            persona,
            memory_ctx,
            envelope,
            decision,
            briefing_text=briefing_text,
            enable_search_tools=(self.web_perception is not None),
            enable_webpage_fetch=bool(self.web_perception and self.web_perception.webpage_fetch_enabled),
            image_blocks=image_blocks or None,
            vision_hints=vision_hints or None,
            correction_hints=correction_hints or None,
            mentioned_users=mentioned_users or None,
        )

        # 8. Invoke Main Brain via Model Control Center (with autonomous Tool Calling)
        t_brain = time.perf_counter()
        search_tools = self.search_orchestrator.active_search_tools()
        try:
            completion = await self.model_hub.chat_complete(
                session=session,
                slot="main_brain",
                messages=messages,
                temperature=0.75,
                max_tokens=600 if decision.action == "direct_pass" else 160,
                source="production",
                request_id=request_id,
                session_scope=session_scope,
                tools=search_tools,
            )
            await self._trace(
                session, request_id, envelope.scene, envelope.group_id, "main_brain", "ok",
                latency_ms=int((time.perf_counter() - t_brain) * 1000),
                meta={
                    "provider": completion.provider_name,
                    "used_fallback": completion.used_fallback,
                    "tool_calls": len(completion.tool_calls),
                    "recalled_episodes": memory_ctx.get("l3_recall_meta", {}).get("recalled_count", len(memory_ctx["l3_episodes"])),
                    "recalled_episode_ids": memory_ctx.get("l3_recall_meta", {}).get("recalled_episode_ids", []),
                    "recall_mode": memory_ctx.get("l3_recall_meta", {}).get("mode", "default"),
                    "vision_cache_hits": vision_cache_hits,
                    "vision_blocks": len(image_blocks),
                },
            )
        except Exception as exc:
            await self._trace(
                session, request_id, envelope.scene, envelope.group_id, "main_brain", "error",
                latency_ms=int((time.perf_counter() - t_brain) * 1000),
                error_type=type(exc).__name__,
            )
            raise

        if await self.store.is_recalled(envelope.message_id):
            return {"handled": "aborted_recalled_before_send", "request_id": request_id, "decision": decision.model_dump()}

        # 8b. Autonomous Web Search in an isolated session (send immediate filler first so chat never stalls)
        search_queries: list[str] = []
        filler_text: str | None = None
        filler_delivery: dict[str, Any] | None = None
        if completion.tool_calls and self.web_perception is not None:
            filler_text = self.search_orchestrator.pick_search_filler(
                completion.content, list(persona.banned_phrases or []), request_id
            )
            if voice_intent != "answer_voice":
                f_out = await self.adapter.send_message(
                    session,
                    envelope.scene,
                    target_id or envelope.sender_qq,
                    filler_text,
                )
                f_out.request_id = request_id
                f_out.feedback_status = "none"
                f_out.gatekeeper_action = decision.action
                f_out.trigger_msg_id = envelope.message_id
                filler_delivery = {
                    "id": f_out.id,
                    "text": filler_text,
                    "status": f_out.status,
                    "onebot_status": f_out.onebot_status,
                }
            completion, search_queries = await self.search_orchestrator.execute_isolated_search_session(
                session=session,
                persona=persona,
                memory_ctx=memory_ctx,
                envelope=envelope,
                decision=decision,
                briefing_text=briefing_text,
                base_messages=messages,
                initial_completion=completion,
                request_id=request_id,
                trace_fn=self._trace,
            )

        total_elapsed_s = time.perf_counter() - t_start
        total_elapsed_ms = int(total_elapsed_s * 1000)
        if total_elapsed_ms > self.ideal_reply_ms:
            await self.model_hub.alert_service.notify(
                session=session,
                severity="warning",
                category="slow_response",
                title=f"回复处理超过理想耗时 ({total_elapsed_ms}ms > {self.ideal_reply_ms}ms)",
                detail=f"请求 {request_id} ({decision.action}) 总耗时 {total_elapsed_ms}ms。",
            )
        if (
            not filler_delivery
            and decision.action == "soft_entry"
            and envelope.scene == "group"
            and envelope.group_id is not None
            and total_elapsed_s >= self.stale_soft_entry_seconds
        ):
            newer_cnt = await self.count_newer_group_messages(session, envelope.group_id, envelope.message_id)
            if newer_cnt >= self.stale_soft_entry_min_msgs:
                await self._trace(
                    session, request_id, envelope.scene, envelope.group_id, "onebot", "aborted_stale_soft_entry",
                    meta={"elapsed_s": round(total_elapsed_s, 1), "newer_msgs": newer_cnt},
                )
                return {"handled": "aborted_stale_soft_entry", "request_id": request_id, "decision": decision.model_dump()}

        # 9. Extract Sticker Tags + Anti-AI-Flavor Sanitization
        text_without_tags, sticker_tags = extract_sticker_tags(completion.content)
        cleaned = sanitize_anti_ai_flavor(text_without_tags, list(persona.banned_phrases or []))

        if pending_vision_phashes and self.multimodal_service is not None and cleaned:
            summary_note = f"图意({(envelope.raw_text or '').strip()[:30] or '无配文'}) -> 回复参考: {cleaned[:80]}"
            for ph in pending_vision_phashes:
                await self.multimodal_service.save_vision_description(session, ph, summary_note)

        # 10. Voice synthesis if requested ('answer_voice')
        if voice_intent == "answer_voice" and self.tts_service is not None:
            t_tts = time.perf_counter()
            mood = memory_ctx["l5_self_state"].get("mood", "neutral")
            tts_res = await self.tts_service.synthesize_and_record(
                session=session,
                scene=envelope.scene,
                target_id=target_id or envelope.sender_qq,
                text=cleaned,
                intent="answer_voice",
                emotion=mood,
                request_id=request_id,
            )
            await self._trace(
                session, request_id, envelope.scene, envelope.group_id, "tts",
                "synthesized" if tts_res["ok"] else "fallback",
                latency_ms=int((time.perf_counter() - t_tts) * 1000),
                error_type=None if tts_res["ok"] else "tts_fallback",
            )
            if tts_res["ok"]:
                t_ob = time.perf_counter()
                out = await self.adapter.send_voice(session, envelope.scene, target_id or envelope.sender_qq, tts_res["audio_b64"])
                out.request_id = request_id
                await self.tts_service.mark_onebot_receipt(session, tts_res["delivery_id"], out.onebot_echo, out.onebot_status)
                await self._trace(
                    session, request_id, envelope.scene, envelope.group_id, "onebot", out.onebot_status,
                    latency_ms=int((time.perf_counter() - t_ob) * 1000),
                )
                if out.status == "sent":
                    return {
                        "handled": "voice_answer",
                        "request_id": request_id,
                        "decision": decision.model_dump(),
                        "bubbles": [cleaned],
                        "tts": {**tts_res, "onebot_status": out.onebot_status, "onebot_echo": out.onebot_echo},
                        "provider": completion.provider_name,
                    }

        # 11. Natural Bubble Splitting + Casual Punctuation Cleanup + Smart Buried-Only Quoting
        should_quote = False
        if envelope.scene == "group" and envelope.group_id is not None:
            should_quote = await self.should_quote_target(
                session=session,
                group_id=envelope.group_id,
                target_message_id=envelope.message_id,
                elapsed_seconds=(time.perf_counter() - t_start),
                after_filler=bool(filler_delivery),
            )
        raw_bubbles = split_natural_bubbles(cleaned, max_bubbles=int(cfg.get("max_bubbles", 3)))
        bubbles = format_casual_bubbles(raw_bubbles, cleaned)
        payload = {
            "request_id": request_id,
            "envelope": envelope,
            "decision": decision,
            "persona": persona,
            "target_id": target_id,
            "should_quote": should_quote,
            "bubbles": bubbles,
            "filler_delivery": filler_delivery,
            "filler_text": filler_text,
            "search_queries": search_queries,
            "sticker_tags": sticker_tags,
            "completion": completion,
        }
        if (
            defer_multi_bubble
            and len(bubbles) > 1
            and self.bubble_delay_max_seconds > 0.0
            and envelope.scene == "group"
            and envelope.group_id is not None
        ):
            return {"_deferred_send": payload}
        return await self._finalize_reply_delivery(session, payload)

    async def _finalize_reply_delivery(
        self,
        session: AsyncSession,
        payload: dict[str, Any],
        group_id: int | None = None,
        generation_id: int | None = None,
    ) -> dict[str, Any]:
        request_id: str = payload["request_id"]
        envelope: MessageEnvelope = payload["envelope"]
        decision: GatekeeperDecision = payload["decision"]
        persona: PersonaVersion = payload["persona"]
        target_id: int | None = payload["target_id"]
        should_quote: bool = payload["should_quote"]
        bubbles: list[str] = payload["bubbles"]
        filler_delivery: dict[str, Any] | None = payload["filler_delivery"]
        filler_text: str | None = payload["filler_text"]
        search_queries: list[str] = payload["search_queries"]
        sticker_tags: list[str] = payload["sticker_tags"]
        completion = payload["completion"]

        t_ob = time.perf_counter()
        deliveries = [filler_delivery] if filler_delivery else []
        sent_bubbles: list[str] = []
        cancelled_stale = False

        for idx, bubble in enumerate(bubbles):
            if generation_id is not None and group_id is not None:
                if idx > 0:
                    delay = self._compute_bubble_delay(bubble)
                    if delay > 0.0:
                        await asyncio.sleep(delay)
                if self._group_generations.get(group_id) != generation_id:
                    cancelled_stale = True
                    break
                if await self.store.is_recalled(envelope.message_id):
                    cancelled_stale = True
                    break

            reply_id = envelope.message_id if (idx == 0 and should_quote) else None
            delivery = await self.adapter.send_message(
                session=session,
                scene=envelope.scene,
                target_id=target_id or envelope.sender_qq,
                text=bubble,
                reply_to_msg_id=reply_id,
            )
            delivery.request_id = request_id
            delivery.gatekeeper_action = decision.action
            delivery.trigger_msg_id = envelope.message_id
            delivery.feedback_status = "pending" if envelope.scene == "group" else "none"
            deliveries.append({"id": delivery.id, "text": bubble, "status": delivery.status, "onebot_status": delivery.onebot_status})
            sent_bubbles.append(bubble)

        receipt_statuses = {delivery["onebot_status"] for delivery in deliveries}
        text_status = next(
            (status for status in ("failed", "unverified", "submitted", "accepted") if status in receipt_statuses),
            "skipped",
        )
        await self._trace(
            session, request_id, envelope.scene, envelope.group_id, "onebot",
            text_status,
            latency_ms=int((time.perf_counter() - t_ob) * 1000),
            meta={"bubbles": len(deliveries), "cancelled_stale": cancelled_stale},
        )
        if not sent_bubbles and cancelled_stale:
            return {"handled": "aborted_stale_generation", "request_id": request_id, "decision": decision.model_dump()}

        sent_sticker = None
        if not cancelled_stale and sticker_tags and self.sticker_service is not None:
            asset = await self.sticker_service.pick_sticker_for_tag(session, sticker_tags[0])
            if asset and (asset.source_url or asset.file_path):
                stk_out = await self.adapter.send_sticker(
                    session,
                    envelope.scene,
                    target_id or envelope.sender_qq,
                    asset.source_url or asset.file_path,
                )
                stk_out.request_id = request_id
                sent_sticker = {"id": asset.id, "tag": sticker_tags[0], "phash": asset.phash}

        bot_qq = envelope.self_qq or self.adapter.bot_qq
        if decision.action == "direct_pass":
            speaker_ident = await session.get(QQIdentity, envelope.sender_qq)
            if speaker_ident is not None:
                speaker_ident.relationship_level = int(speaker_ident.relationship_level or 0) + 1
        if bot_qq and sent_bubbles:
            await self.identity_service.upsert_from_message(
                session, qq_id=bot_qq, nickname=persona.bot_name, group_id=envelope.group_id, group_card=persona.bot_name
            )
            session.add(
                ChatMessage(
                    message_id=f"bot_{request_id}",
                    scene=envelope.scene,
                    group_id=envelope.group_id,
                    sender_qq=bot_qq,
                    sender_display_name=persona.bot_name,
                    raw_text="\n".join(sent_bubbles),
                    expanded_reply_text=f"{envelope.display_name}: {envelope.raw_text}" if should_quote else None,
                    gatekeeper_decision="bot_reply",
                    gatekeeper_score=1.0,
                    is_recalled=False,
                    is_ambient_only=False,
                )
            )
            await session.flush()
            if envelope.scene == "group" and envelope.group_id is not None:
                await self.store.record_group_bot_activity(
                    envelope.group_id,
                    ttl_seconds=max(180, int(self.gatekeeper.group_topic_warmth_seconds) + 30),
                )

        await self.event_bus.emit(
            "on_reply_sent",
            {"request_id": request_id, "scene": envelope.scene, "target_id": target_id, "bubbles": sent_bubbles, "sticker": sent_sticker},
        )
        return {
            "handled": decision.action,
            "request_id": request_id,
            "decision": decision.model_dump(),
            "bubbles": sent_bubbles,
            "deliveries": deliveries,
            "filler": filler_text,
            "search_queries": search_queries,
            "sticker": sent_sticker,
            "cancelled_stale": cancelled_stale,
            "provider": completion.provider_name,
            "used_fallback": completion.used_fallback,
        }

    def _schedule_background_image_caption(self, img_items: list[dict[str, Any]]) -> None:
        if self._session_factory is None or self.multimodal_service is None:
            return
        task = asyncio.create_task(self.caption_image_items(img_items), name="bg_image_caption")
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def caption_image_items(
        self,
        img_items: list[dict[str, Any]],
        session: AsyncSession | None = None,
    ) -> int:
        """Download and generate concise 15-30 char captions into ImageVisionCache (dHash deduped)."""
        if self.multimodal_service is None:
            return 0
        if session is None and self._session_factory is None:
            return 0

        async def _run(s: AsyncSession) -> int:
            fetcher = getattr(self.multimodal_service, "image_fetcher", None)
            captioned = 0
            for item in img_items[:2]:
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                existing = (
                    await s.execute(select(ImageVisionCache).where(ImageVisionCache.source_url == url).limit(1))
                ).scalar_one_or_none()
                if existing is not None and existing.description:
                    continue
                _, img_bytes = await resolve_image_phash(url, timeout=3.0, fetcher=fetcher)
                if img_bytes is None:
                    continue
                prep = await self.multimodal_service.lookup_or_prepare(
                    s, img_bytes, source_url=url, is_gif=url.lower().endswith(".gif")
                )
                if prep.get("cache_hit") and prep.get("description"):
                    continue
                block = prep.get("content_block")
                if not block:
                    continue
                try:
                    comp = await self.model_hub.chat_complete(
                        session=s,
                        slot="main_brain",
                        messages=[
                            {
                                "role": "system",
                                "content": "你是图片摘要助手。用15-30字客观概括图片内容（如游戏界面、聊天截图、表情包等），只输出一句话摘要，不要多余解释。",
                            },
                            {"role": "user", "content": [block]},
                        ],
                        temperature=0.2,
                        max_tokens=60,
                        source="vision_caption",
                    )
                    summary = (comp.content or "").strip().splitlines()[0][:80]
                    if summary:
                        await self.multimodal_service.save_vision_description(s, str(prep["phash"]), summary)
                        captioned += 1
                except Exception as exc:
                    logger.debug("Background image caption failed for %s: %s", url, exc)
            return captioned

        async with self._bg_caption_sem:
            try:
                if session is not None:
                    return await _run(session)
                async with self._session_factory() as bg_session:
                    count = await _run(bg_session)
                    await bg_session.commit()
                    return count
            except Exception as exc:
                logger.debug("Background image caption task failed: %s", exc)
                return 0

    @staticmethod
    def _gatekeeper_reason_code(decision: GatekeeperDecision) -> str:
        """Map GatekeeperDecision reason into a privacy-safe diagnostic code (zero raw chat text)."""
        r = decision.reason or ""
        if "recalled" in r:
            return "recalled"
        if "Daily budget circuit breaker" in r:
            return "budget_fuse"
        if "Budget fuse active" in r:
            return "budget_admin_override"
        if "Explicit @mention" in r:
            return "explicit_at_or_reply"
        if "Direct alias call" in r:
            return "direct_alias"
        if "Hard rule: message explicitly @mentions" in r:
            return "other_user_mention"
        if "Active peer-to-peer" in r:
            return "active_peer_thread"
        if "Air guardrail" in r:
            return "air_guardrail_attention" if "attention" in r else "air_guardrail"
        if "Private DM" in r:
            return "private_dm"
        if "attention=" in r:
            return "attention_passed" if decision.action == "soft_entry" else "attention_filtered"
        if "warmth=" in r:
            return "group_warmth_passed" if decision.action == "soft_entry" else "group_warmth_filtered"
        if decision.target == "other_user":
            return "llm_other_user"
        return "soft_entry_passed" if decision.action == "soft_entry" else "threshold_filtered"

    async def count_newer_group_messages(
        self,
        session: AsyncSession,
        group_id: int,
        target_message_id: str,
    ) -> int:
        target_row = (
            await session.execute(
                select(ChatMessage.id)
                .where(ChatMessage.group_id == group_id, ChatMessage.message_id == target_message_id)
                .order_by(ChatMessage.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if target_row is None:
            return 0
        return int(
            (
                await session.execute(
                    select(_sql_func.count(ChatMessage.id)).where(
                        ChatMessage.group_id == group_id,
                        ChatMessage.scene == "group",
                        ChatMessage.id > target_row,
                    )
                )
            ).scalar_one()
            or 0
        )

    async def should_quote_target(
        self,
        session: AsyncSession,
        group_id: int,
        target_message_id: str,
        elapsed_seconds: float = 0.0,
        after_filler: bool = False,
    ) -> bool:
        """Smart Quoting: quote if buried by >=N newer messages, reply took >=25s, or newer msgs arrived after filler."""
        if elapsed_seconds >= self.smart_quote_min_delay_seconds:
            return True
        newer_count = await self.count_newer_group_messages(session, group_id, target_message_id)
        if after_filler and newer_count >= 1:
            return True
        return newer_count >= self.smart_quote_min_intervening
