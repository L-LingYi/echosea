"""Main Brain Service (Complete v1.0):
Prompt Cache static prefix + 5-Layer Memory + Slow/Fast Internet Perception
+ 4 Anti-AI-Flavor defenses + Sticker Tag Trigger + Doubao TTS 3-Intent Voice Pipeline.
"""
from __future__ import annotations

import json
import logging
import re
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
from app.core.web_perception_service import WebPerceptionService, needs_fast_web_search
from app.models.db import ChatMessage, PersonaVersion, SystemConfig
from app.models.schemas import GatekeeperDecision, MessageEnvelope

logger = logging.getLogger(__name__)

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
    """End-to-end orchestrator across all 10 subsystems."""

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
        self._group_msg_counters: dict[int, int] = {}

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
        post_type = raw_event.get("post_type")

        if post_type == "notice" and raw_event.get("notice_type") in ("group_recall", "friend_recall"):
            msg_id = str(raw_event.get("message_id", ""))
            if msg_id:
                await self.store.mark_recalled(msg_id)
                updated = await self.memory_service.mark_message_recalled(session, msg_id)
                await self.event_bus.emit("on_message_recalled", {"message_id": msg_id})
                return {"handled": "recall", "message_id": msg_id, "updated_rows": updated}
            return {"handled": "recall_ignored"}

        envelope = await self.adapter.normalize_event(raw_event, session=session)
        if envelope is None:
            return {"handled": "ignored"}

        return await self.process_envelope(session, envelope)

    async def process_envelope(self, session: AsyncSession, envelope: MessageEnvelope) -> dict[str, Any]:
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
            return {"handled": "disabled_by_config"}

        replied_to_bot = bool(envelope.self_qq and envelope.quoted_sender_qq == envelope.self_qq)
        if envelope.reply_to_msg_id and envelope.self_qq:
            q_stmt = select(ChatMessage).where(
                ChatMessage.message_id == envelope.reply_to_msg_id,
                ChatMessage.scene == envelope.scene,
                ChatMessage.group_id == envelope.group_id,
                ChatMessage.is_recalled.is_(False),
            ).limit(1)
            quoted = (await session.execute(q_stmt)).scalar_one_or_none()
            if quoted and quoted.sender_qq == envelope.self_qq:
                replied_to_bot = True

        # 3. Three-Layer Gatekeeper Evaluation
        budget_fuse = await self.model_hub.is_budget_fuse_active(session)
        self.gatekeeper.soft_entry_threshold = float(cfg.get("soft_entry_threshold", self.gatekeeper.soft_entry_threshold))

        decision = await self.gatekeeper.evaluate(
            envelope=envelope,
            bot_name=persona.bot_name,
            aliases=list(persona.aliases or []),
            replied_to_bot=replied_to_bot,
            budget_fuse_active=budget_fuse,
            admin_qq=self.admin_qq,
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
            {"envelope": envelope.model_dump(mode="json"), "decision": decision.model_dump()},
        )

        if decision.action in ("silent", "blocked_recall"):
            return {"handled": decision.action, "decision": decision.model_dump()}

        # 5. Anti-risk rate limiting (per-group + global)
        target_id = envelope.group_id if envelope.scene == "group" else envelope.sender_qq
        if envelope.scene == "group" and envelope.group_id:
            if not await self.store.check_rate_limit(f"group:{envelope.group_id}", self.group_rate_limit_per_min):
                return {"handled": "rate_limited_group", "decision": decision.model_dump()}
        if not await self.store.check_rate_limit("global", self.global_rate_limit_per_min):
            return {"handled": "rate_limited_global", "decision": decision.model_dump()}

        # 6. Check Doubao TTS 3-Intent Voice Shortcut ('read_quote' reads quoted text directly!)
        voice_intent = detect_voice_intent(envelope.raw_text, has_reply_quote=bool(envelope.expanded_reply_text))
        quoted_text = envelope.quoted_text or envelope.expanded_reply_text
        if voice_intent == "read_quote" and quoted_text and self.tts_service is not None:
            tts_res = await self.tts_service.synthesize_and_record(
                session=session,
                scene=envelope.scene,
                target_id=target_id or envelope.sender_qq,
                text=quoted_text,
                intent="read_quote",
            )
            if tts_res["ok"]:
                delivery = await self.adapter.send_voice(session, envelope.scene, target_id or envelope.sender_qq, tts_res["audio_b64"])
                if delivery.status == "sent":
                    return {"handled": "voice_read_quote", "decision": decision.model_dump(), "tts": tts_res}
            # Automatic fallback to text
            await self.adapter.send_message(session, envelope.scene, target_id or envelope.sender_qq, quoted_text)
            return {"handled": "voice_fallback_text", "decision": decision.model_dump(), "tts": tts_res}

        # 7. Assemble 5-Layer Memory + Slow/Fast Internet Perception
        memory_ctx = await self.memory_service.assemble_context(
            session=session,
            scene=envelope.scene,
            sender_qq=envelope.sender_qq,
            group_id=envelope.group_id,
        )
        briefing_text = ""
        fast_search_text = ""
        if self.web_perception is not None:
            briefing_text = await self.web_perception.get_latest_briefing_text(session)
            if needs_fast_web_search(envelope.raw_text):
                fast_search_text = await self.web_perception.fast_search(session, envelope.raw_text)

        messages = self.build_prompt_messages(
            persona,
            memory_ctx,
            envelope,
            decision,
            briefing_text=briefing_text,
            fast_search_text=fast_search_text,
        )

        # 8. Invoke Main Brain via Model Control Center (with Primary -> Fallback failover)
        completion = await self.model_hub.chat_complete(
            session=session,
            slot="main_brain",
            messages=messages,
            temperature=0.75,
            max_tokens=300 if decision.action == "direct_pass" else 120,
        )

        if await self.store.is_recalled(envelope.message_id):
            return {"handled": "aborted_recalled_before_send", "decision": decision.model_dump()}

        # 9. Extract Sticker Tags + Anti-AI-Flavor Sanitization
        text_without_tags, sticker_tags = extract_sticker_tags(completion.content)
        cleaned = sanitize_anti_ai_flavor(text_without_tags, list(persona.banned_phrases or []))

        # 10. Voice synthesis if requested ('answer_voice'), unless user said 'force_text'
        if voice_intent == "answer_voice" and self.tts_service is not None:
            mood = memory_ctx["l5_self_state"].get("mood", "neutral")
            tts_res = await self.tts_service.synthesize_and_record(
                session=session,
                scene=envelope.scene,
                target_id=target_id or envelope.sender_qq,
                text=cleaned,
                intent="answer_voice",
                emotion=mood,
            )
            if tts_res["ok"]:
                delivery = await self.adapter.send_voice(session, envelope.scene, target_id or envelope.sender_qq, tts_res["audio_b64"])
                if delivery.status == "sent":
                    return {
                        "handled": "voice_answer",
                        "decision": decision.model_dump(),
                        "bubbles": [cleaned],
                        "tts": tts_res,
                        "provider": completion.provider_name,
                    }

        # 11. Natural Bubble Splitting + Outbox Delivery + Optional Sticker Trigger
        bubbles = split_natural_bubbles(cleaned, max_bubbles=int(cfg.get("max_bubbles", 3)))
        deliveries = []
        for idx, bubble in enumerate(bubbles):
            reply_id = envelope.message_id if (idx == 0 and envelope.scene == "group") else None
            delivery = await self.adapter.send_message(
                session=session,
                scene=envelope.scene,
                target_id=target_id or envelope.sender_qq,
                text=bubble,
                reply_to_msg_id=reply_id,
            )
            deliveries.append({"id": delivery.id, "text": bubble, "status": delivery.status})

        sent_sticker = None
        if sticker_tags and self.sticker_service is not None:
            asset = await self.sticker_service.pick_sticker_for_tag(session, sticker_tags[0])
            if asset and (asset.source_url or asset.file_path):
                await self.adapter.send_sticker(
                    session,
                    envelope.scene,
                    target_id or envelope.sender_qq,
                    asset.source_url or asset.file_path,
                )
                sent_sticker = {"id": asset.id, "tag": sticker_tags[0], "phash": asset.phash}

        await self.event_bus.emit(
            "on_reply_sent",
            {"scene": envelope.scene, "target_id": target_id, "bubbles": bubbles, "sticker": sent_sticker},
        )
        return {
            "handled": decision.action,
            "decision": decision.model_dump(),
            "bubbles": bubbles,
            "deliveries": deliveries,
            "sticker": sent_sticker,
            "provider": completion.provider_name,
            "used_fallback": completion.used_fallback,
        }

    @staticmethod
    def build_prompt_messages(
        persona: PersonaVersion,
        memory_ctx: dict[str, Any],
        envelope: MessageEnvelope,
        decision: GatekeeperDecision,
        briefing_text: str = "",
        fast_search_text: str = "",
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
            recent = "\n".join(
                f"{m['sender']}: {m['text']}" + (f" (引用: {m['reply_to']})" if m.get("reply_to") else "")
                for m in memory_ctx["l1_working"][-12:]
            )
            dynamic_parts.append(f"[近期对话记录]\n{recent}")

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
