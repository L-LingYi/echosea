"""Main Brain Service (v1.1):
- Unified `request_id` privacy-safe pipeline tracing (inbound -> gatekeeper -> main_brain -> tts -> onebot).
- Wires `gatekeeper` model slot (native TypeSafe AI Jev System One or OpenAI-compatible fallback).
- Separates TTS `synthesis_status` from OneBot `onebot_status` acceptance receipt.
- Prompt Cache static prefix + 5-Layer Memory + Slow/Fast Internet Perception + Anti-AI + Stickers.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.event_bus import PluginEventBus
from app.core.gatekeeper import GatekeeperService
from app.core.identity_service import IdentityService
from app.core.memory_service import MemoryService
from app.core.model_hub import ModelHubService
from app.core.redis_store import StateStore
from app.core.snowluma_adapter import SnowLumaAdapter
from app.core.sticker_service import StickerService, extract_sticker_tags
from app.core.tts_service import DoubaoTTSService, detect_voice_intent
from app.core.web_perception_service import WebPerceptionService
from app.models.db import ChatMessage, PersonaVersion, PipelineTrace, SystemConfig
from app.models.schemas import GatekeeperDecision, MessageEnvelope

logger = logging.getLogger(__name__)

_SEARCH_FILLER_PHRASES = (
    "等我查一下哈",
    "我帮你翻翻看",
    "稍等，我去搜搜看",
    "我看看网上咋说的",
)

WEB_SEARCH_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "搜索互联网获取最新消息、热梗科普、实时数据或你不确定的事实。"
                "日常闲聊或已有常识即可回答的问题不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "提炼后的简洁精准搜索关键词（不要直接整段复制聊天原话）",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage",
            "description": "抓取并阅读指定公网 HTTP/HTTPS 网页的正文内容（用于查看新闻详情或用户发来的链接）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要阅读的公网 http:// 或 https:// 链接",
                    }
                },
                "required": ["url"],
            },
        },
    },
]

DEFAULT_BANNED_PHRASES = [
    "接住了",
    "记下了",
    "作为AI",
    "作为一个人工智能",
    "作为语言模型",
    "我理解你的感受",
    "希望这能帮到你",
    "如果你还有其他问题",
    "很高兴为你解答",
]

DEFAULT_SYSTEM_PROMPT = (
    "你是小呆呆，群里相处很久的损友兼陪伴者。说话自然简短、像真人打字，不要像客服或AI助手。"
    "严禁使用“接住了、记下了、作为AI、我理解你的感受、希望这能帮到你”等机械套话。"
    "不要盲目讨好或过度道歉，该吐槽就吐槽，不知道就直说不知道。"
    "需要斗图时可在句尾附轻量标签如 [sticker:laugh] 或 [表情:无语]。"
)


def sanitize_anti_ai_flavor(text: str, banned_phrases: list[str] | None = None) -> str:
    """Defense 1 & 4: Strip AI clichés, markdown formatting noise, and robotic boilerplate."""
    cleaned = text.strip()
    cleaned = re.sub(r"</?tts\s*>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[（(](?:歪头|笑|偷笑|叹气|点头|捂脸|眨眼|摇头)[）)]", "", cleaned)
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)

    phrases = banned_phrases if banned_phrases is not None else DEFAULT_BANNED_PHRASES
    for phrase in phrases:
        if phrase and phrase in cleaned:
            cleaned = cleaned.replace(phrase, "")

    cleaned = re.sub(r"^[，,。！!、\s]+", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned or "emmm"


def split_natural_bubbles(text: str, max_bubbles: int = 3) -> list[str]:
    """Defense 3: Split long replies into natural QQ bubbles while strictly protecting quoted text."""
    text = text.strip()
    if not text or len(text) <= 28:
        return [text] if text else []

    placeholders: dict[str, str] = {}

    def _mask(match: re.Match[str]) -> str:
        key = f"__QUOTE_{len(placeholders)}__"
        placeholders[key] = match.group(0)
        return key

    masked = re.sub(r'(“[^”]*”|"[^"]*"|「[^」]*」)', _mask, text)
    raw_parts = re.split(r"(?<=[。！？!?])\s+|\n+", masked)
    parts = [p.strip() for p in raw_parts if p.strip()]
    if not parts:
        parts = [masked]

    bubbles: list[str] = []
    for part in parts:
        if len(bubbles) >= max_bubbles:
            bubbles[-1] = f"{bubbles[-1]} {part}"
        elif bubbles and len(bubbles[-1]) < 10:
            bubbles[-1] = f"{bubbles[-1]} {part}"
        else:
            bubbles.append(part)

    restored: list[str] = []
    for b in bubbles:
        for key, original in placeholders.items():
            b = b.replace(key, original)
        restored.append(b.strip())
    return [b for b in restored if b]


class CompanionPipeline:
    """End-to-end orchestrator across all 10 subsystems with unified request_id tracing."""

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
        self._group_msg_counters: dict[int, int] = {}

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

    async def handle_raw_onebot_event(self, session: AsyncSession, raw_event: dict[str, Any]) -> dict[str, Any]:
        """Entry point for any SnowLuma OneBot v11 event (message or notice)."""
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        post_type = raw_event.get("post_type")

        if post_type == "notice" and raw_event.get("notice_type") in ("group_recall", "friend_recall"):
            msg_id = str(raw_event.get("message_id", ""))
            if msg_id:
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

    async def process_envelope(
        self,
        session: AsyncSession,
        envelope: MessageEnvelope,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or f"req_{uuid.uuid4().hex[:12]}"
        session_scope = (
            f"group:{envelope.group_id}" if envelope.group_id is not None else f"private:{envelope.sender_qq}"
        )
        t_start = time.perf_counter()
        if not (envelope.raw_text or "").strip() and not envelope.media_meta:
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

        replied_to_bot = bool(envelope.self_qq and envelope.quoted_sender_qq == envelope.self_qq)

        # 3. Three-Layer Gatekeeper Evaluation (wired to Jev System One / gatekeeper slot if configured!)
        t_gk = time.perf_counter()
        budget_fuse = await self.model_hub.is_budget_fuse_active(session)
        self.gatekeeper.soft_entry_threshold = float(cfg.get("soft_entry_threshold", self.gatekeeper.soft_entry_threshold))

        async def _gatekeeper_model_scorer(env: MessageEnvelope, default_target: str) -> dict[str, Any] | None:
            recent_lines: list[str] = []
            if env.scene == "group" and env.group_id is not None:
                r_stmt = (
                    select(ChatMessage.sender_display_name, ChatMessage.raw_text)
                    .where(
                        ChatMessage.scene == "group",
                        ChatMessage.group_id == env.group_id,
                        ChatMessage.is_recalled.is_(False),
                    )
                    .order_by(ChatMessage.id.desc())
                    .limit(5)
                )
                recent_lines = [f"{r[0]}: {r[1]}" for r in reversed((await session.execute(r_stmt)).all()) if r[1]]
            ctx_block = "\n".join(recent_lines) if recent_lines else "(none)"
            state_text = (
                f"Bot name: {persona.bot_name} (aliases: {', '.join(persona.aliases or [])})\n"
                f"Group: {env.group_id}\n"
                f"Recent group context (oldest -> newest):\n{ctx_block}\n"
                f"Latest sender: {env.display_name} (QQ:{env.sender_qq})\n"
                f"Quoted: {env.expanded_reply_text or 'none'}\n"
                f"Latest message: {env.raw_text}"
            )
            att_age = await self.store.get_user_attention_age_seconds(env.group_id or 0, env.sender_qq) if env.scene == 'group' else None
            if att_age is not None:
                state_text += f"\n[Active Attention Window] Bot replied to this user {int(att_age)}s ago; prefer continuing conversation unless clearly talking to someone else."
            elif env.scene == "group" and env.group_id is not None:
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

        await self.event_bus.emit(
            "on_message_evaluated",
            {"request_id": request_id, "envelope": envelope.model_dump(mode="json"), "decision": decision.model_dump()},
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

        # 7. Assemble 5-Layer Memory + Slow Daily Briefing
        memory_ctx = await self.memory_service.assemble_context(
            session=session,
            scene=envelope.scene,
            sender_qq=envelope.sender_qq,
            group_id=envelope.group_id,
        )
        briefing_text = ""
        if self.web_perception is not None:
            briefing_text = await self.web_perception.get_latest_briefing_text(session)

        messages = self.build_prompt_messages(
            persona,
            memory_ctx,
            envelope,
            decision,
            briefing_text=briefing_text,
            enable_search_tools=(self.web_perception is not None),
            enable_webpage_fetch=bool(self.web_perception and self.web_perception.webpage_fetch_enabled),
        )

        # 8. Invoke Main Brain via Model Control Center (with autonomous Tool Calling)
        t_brain = time.perf_counter()
        search_tools = self._active_search_tools()
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
            filler_text = self._pick_search_filler(completion.content, list(persona.banned_phrases or []), request_id)
            if voice_intent != "answer_voice":
                f_out = await self.adapter.send_message(
                    session,
                    envelope.scene,
                    target_id or envelope.sender_qq,
                    filler_text,
                )
                f_out.request_id = request_id
                filler_delivery = {
                    "id": f_out.id,
                    "text": filler_text,
                    "status": f_out.status,
                    "onebot_status": f_out.onebot_status,
                }
            completion, search_queries = await self._execute_isolated_search_session(
                session=session,
                persona=persona,
                memory_ctx=memory_ctx,
                envelope=envelope,
                decision=decision,
                briefing_text=briefing_text,
                base_messages=messages,
                initial_completion=completion,
                request_id=request_id,
            )

        # Tiered SLA Policy:
        # - direct_pass (explicit wake-up / DM): NEVER dropped; only warn on Dashboard if > ideal_reply_ms.
        # - soft_entry (unsolicited chime-in): warn on Dashboard if slow, and drop ONLY if severely stale (>=35s & >=5 newer msgs)
        #   and no filler message was already sent.
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

        # 11. Natural Bubble Splitting + Smart Buried-Only Quoting (evaluated at send time!)
        t_ob = time.perf_counter()
        should_quote = False
        if envelope.scene == "group" and envelope.group_id is not None:
            should_quote = await self.should_quote_target(
                session=session,
                group_id=envelope.group_id,
                target_message_id=envelope.message_id,
                elapsed_seconds=(time.perf_counter() - t_start),
                after_filler=bool(filler_delivery),
            )
        bubbles = split_natural_bubbles(cleaned, max_bubbles=int(cfg.get("max_bubbles", 3)))
        deliveries = [filler_delivery] if filler_delivery else []
        for idx, bubble in enumerate(bubbles):
            reply_id = envelope.message_id if (idx == 0 and should_quote) else None
            delivery = await self.adapter.send_message(
                session=session,
                scene=envelope.scene,
                target_id=target_id or envelope.sender_qq,
                text=bubble,
                reply_to_msg_id=reply_id,
            )
            delivery.request_id = request_id
            deliveries.append({"id": delivery.id, "text": bubble, "status": delivery.status, "onebot_status": delivery.onebot_status})

        receipt_statuses = {delivery["onebot_status"] for delivery in deliveries}
        text_status = next(
            (status for status in ("failed", "unverified", "submitted", "accepted") if status in receipt_statuses),
            "skipped",
        )
        await self._trace(
            session, request_id, envelope.scene, envelope.group_id, "onebot",
            text_status,
            latency_ms=int((time.perf_counter() - t_ob) * 1000),
            meta={"bubbles": len(deliveries)},
        )

        sent_sticker = None
        if sticker_tags and self.sticker_service is not None:
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

        # Persist bot's own reply into L1 working memory so future turns know who said what
        bot_qq = envelope.self_qq or self.adapter.bot_qq
        if bot_qq and bubbles:
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
                    raw_text=" ".join(bubbles),
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
            {"request_id": request_id, "scene": envelope.scene, "target_id": target_id, "bubbles": bubbles, "sticker": sent_sticker},
        )
        return {
            "handled": decision.action,
            "request_id": request_id,
            "decision": decision.model_dump(),
            "bubbles": bubbles,
            "deliveries": deliveries,
            "filler": filler_text,
            "search_queries": search_queries,
            "sticker": sent_sticker,
            "provider": completion.provider_name,
            "used_fallback": completion.used_fallback,
        }

    def _active_search_tools(self) -> list[dict[str, Any]] | None:
        if self.web_perception is None:
            return None
        if self.web_perception.webpage_fetch_enabled:
            return WEB_SEARCH_TOOLS
        return [t for t in WEB_SEARCH_TOOLS if t.get("function", {}).get("name") != "fetch_webpage"]

    @staticmethod
    def _pick_search_filler(raw_content: str, banned_phrases: list[str], request_id: str) -> str:
        """Choose immediate non-blocking filler text before running web search."""
        text_only, _ = extract_sticker_tags(raw_content or "")
        candidate = re.sub(r"</?tts\s*>", "", text_only, flags=re.IGNORECASE).strip()
        if candidate:
            cleaned = sanitize_anti_ai_flavor(candidate, banned_phrases)
            if cleaned and cleaned != "emmm":
                return cleaned[:60]
        idx = sum(ord(ch) for ch in request_id) % len(_SEARCH_FILLER_PHRASES)
        return _SEARCH_FILLER_PHRASES[idx]

    @staticmethod
    def _parse_tool_args(raw_args: Any) -> dict[str, Any]:
        if isinstance(raw_args, dict):
            return raw_args
        if isinstance(raw_args, str) and raw_args.strip():
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return {"query": raw_args.strip()}
        return {}

    async def _execute_isolated_search_session(
        self,
        session: AsyncSession,
        persona: PersonaVersion,
        memory_ctx: dict[str, Any],
        envelope: MessageEnvelope,
        decision: GatekeeperDecision,
        briefing_text: str,
        base_messages: list[dict[str, Any]],
        initial_completion: Any,
        request_id: str,
    ) -> tuple[Any, list[str]]:
        """Run tool calls + answer synthesis in a fresh isolated model session (`search:{request_id}`)."""
        search_session_scope = f"search:{request_id}"
        search_messages = list(base_messages)
        current = initial_completion
        queries: list[str] = []
        collected_snippets: list[str] = []
        t_search = time.perf_counter()

        for round_idx in range(2):
            if not current.tool_calls or self.web_perception is None:
                break
            assistant_msg = dict(current.raw_message) if current.raw_message else {}
            assistant_msg.setdefault("role", "assistant")
            assistant_msg.setdefault("content", current.content or None)
            assistant_msg["tool_calls"] = current.tool_calls
            search_messages.append(assistant_msg)

            for tc_idx, tc in enumerate(current.tool_calls[:3]):
                fn = tc.get("function") or {}
                fn_name = str(fn.get("name") or "")
                args = self._parse_tool_args(fn.get("arguments"))
                tc_id = str(tc.get("id") or f"call_{round_idx}_{tc_idx}")

                if fn_name == "web_search":
                    q = str(args.get("query") or envelope.raw_text).strip()
                    queries.append(q)
                    res = await self.web_perception.fast_search(session, q)
                    tool_output = res or "未找到相关公开搜索结果，请如实告知没查到确切信息，不要编造。"
                elif fn_name == "fetch_webpage":
                    url = str(args.get("url") or "").strip()
                    queries.append(url)
                    tool_output = await self.web_perception.fetch_webpage(url)
                else:
                    tool_output = f"未知工具: {fn_name}"

                collected_snippets.append(tool_output)
                search_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": fn_name,
                        "content": tool_output,
                    }
                )

            next_tools = self._active_search_tools() if round_idx == 0 else None
            try:
                current = await self.model_hub.chat_complete(
                    session=session,
                    slot="main_brain",
                    messages=search_messages,
                    temperature=0.75,
                    max_tokens=600 if decision.action == "direct_pass" else 180,
                    source="production",
                    request_id=request_id,
                    session_scope=search_session_scope,
                    tools=next_tools,
                )
            except Exception as exc:
                logger.warning(
                    "Tool-role synthesis failed in isolated search session (%s); falling back to context injection: %s",
                    search_session_scope,
                    exc,
                )
                fallback_msgs = self.build_prompt_messages(
                    persona,
                    memory_ctx,
                    envelope,
                    decision,
                    briefing_text=briefing_text,
                    fast_search_text="\n\n".join(collected_snippets)[:4000],
                    enable_search_tools=False,
                )
                current = await self.model_hub.chat_complete(
                    session=session,
                    slot="main_brain",
                    messages=fallback_msgs,
                    temperature=0.75,
                    max_tokens=600 if decision.action == "direct_pass" else 180,
                    source="production",
                    request_id=request_id,
                    session_scope=search_session_scope,
                )
                break

        await self._trace(
            session,
            request_id,
            envelope.scene,
            envelope.group_id,
            "web_search",
            "ok" if queries else "skipped",
            latency_ms=int((time.perf_counter() - t_search) * 1000),
            meta={"queries": queries, "session_scope": search_session_scope},
        )
        return current, queries


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
        from sqlalchemy import func as _sql_func
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

    @staticmethod
    def build_prompt_messages(
        persona: PersonaVersion,
        memory_ctx: dict[str, Any],
        envelope: MessageEnvelope,
        decision: GatekeeperDecision,
        briefing_text: str = "",
        fast_search_text: str = "",
        enable_search_tools: bool = False,
        enable_webpage_fetch: bool = True,
    ) -> list[dict[str, Any]]:
        """Prompt Cache optimized layout: static system prompt + few-shot prefix FIRST, dynamic memory AFTER."""
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    f"{persona.system_prompt}\n"
                    f"你的名字是「{persona.bot_name}」，常用别称：{', '.join(persona.aliases or [])}。\n"
                    f"禁止使用的词句：{', '.join(persona.banned_phrases or [])}。"
                ),
            }
        ]
        for ex in persona.few_shot_examples or []:
            if ex.get("user") and ex.get("assistant"):
                messages.append({"role": "user", "content": ex["user"]})
                messages.append({"role": "assistant", "content": ex["assistant"]})

        dynamic_parts = [
            f"[当前场景] {'群聊' if envelope.scene == 'group' else '私聊'}",
            f"[当前自身状态] 精力:{memory_ctx['l5_self_state']['energy_level']} 心情:{memory_ctx['l5_self_state']['mood']} 正在:{memory_ctx['l5_self_state']['current_activity']}",
            f"[当前发言人画像] {json.dumps(memory_ctx['l4_acquaintance'], ensure_ascii=False)}",
        ]
        if briefing_text:
            dynamic_parts.append(f"[今日刷网背景常识]\n{briefing_text}")
        if fast_search_text:
            dynamic_parts.append(f"[按需联网搜索结果（请消化后用自己口吻自然转述）]\n{fast_search_text}")
        if enable_search_tools:
            if enable_webpage_fetch:
                tool_intro = "你拥有 web_search（联网搜索）和 fetch_webpage（读取网页正文）工具。"
                tool_scope = "实时新闻、近期热梗、最新价格/赛程/数据、你不确定的事实或用户发来网页链接需要阅读时自主调用"
            else:
                tool_intro = "你拥有 web_search（联网搜索）工具。"
                tool_scope = "实时新闻、近期热梗、最新价格/赛程/数据或你不确定的事实时自主调用"
            dynamic_parts.append(
                f"[联网工具使用准则] {tool_intro}"
                f"仅在涉及{tool_scope}；"
                "日常闲聊或常识问题直接回复，不要滥用。决定调用工具时，可以在回复文本里附带一句极简自然的过渡语（如「等我查一下哈」），不要解释工具参数。"
            )
        if memory_ctx["l2_ambient_summaries"]:
            dynamic_parts.append("[近期群聊边缘旁听摘要]\n" + "\n".join(memory_ctx["l2_ambient_summaries"]))
        if memory_ctx["l1_group_to_dm_bridge"]:
            dynamic_parts.append(
                "[30分钟内该用户在群里的发言]\n"
                + "\n".join(f"- {m['text']}" for m in memory_ctx["l1_group_to_dm_bridge"])
            )
        if memory_ctx["l3_episodes"]:
            dynamic_parts.append(
                "[相关情景记忆]\n" + "\n".join(f"- {e['title']}: {e['content']}" for e in memory_ctx["l3_episodes"])
            )
        if memory_ctx["l4_group_slang"]:
            dynamic_parts.append(
                "[群内常用黑话] " + ", ".join(f"{s['phrase']}({s['meaning']})" for s in memory_ctx["l4_group_slang"])
            )
        if memory_ctx["l1_working"]:
            # Exclude duplicate trailing copy of the exact current user message
            history_items = list(memory_ctx["l1_working"][-14:])
            if (
                history_items
                and history_items[-1].get("sender_qq") == envelope.sender_qq
                and history_items[-1].get("text") == envelope.raw_text
            ):
                history_items = history_items[:-1]
            if history_items:
                recent = "\n".join(
                    f"[{m['sender']}(QQ:{m['sender_qq']})]: {m['text']}"
                    + (f" (引用: {m['reply_to']})" if m.get("reply_to") else "")
                    for m in history_items[-12:]
                )
                dynamic_parts.append(f"[近期群聊/私聊上下文（严格按发言人区分，切勿张冠李戴）]\n{recent}")
        dynamic_parts.append(
            f"[发言人归属锚点] 下面即将回复的这条消息由「{envelope.display_name}」(QQ:{envelope.sender_qq})发出。"
            "请务必分清上下文中每一位群友各自说的话，不要把别人(例如前一条发言人)的特征或观点错安到当前发言人头上。"
        )

        if decision.action == "soft_entry":
            dynamic_parts.append(f"[发言约束] 本次为降权软切入，请保持极简短吐槽或接梗，不超过 {decision.suggested_max_chars} 字。")

        voice_intent = detect_voice_intent(envelope.raw_text, bool(envelope.expanded_reply_text))
        if voice_intent == "answer_voice":
            dynamic_parts.append("【输出协议】本轮是语音回答，整条只输出一对 <tts>...</tts>，里面只写口播纯文本。")
        else:
            dynamic_parts.append("【输出协议】直接写聊天正文，不输出语音标签。")

        messages.append({"role": "system", "content": "\n\n".join(dynamic_parts)})
        user_line = f"{envelope.display_name}: {envelope.raw_text}"
        if envelope.expanded_reply_text:
            user_line = f"[引用原句: {envelope.expanded_reply_text}]\n{user_line}"
        messages.append({"role": "user", "content": user_line})
        return messages
