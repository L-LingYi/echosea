"""Three-Layer Attribution Gatekeeper with Soft-Entry Downweight & Air Guardrail.

Key rules from Companion System v1.0 Spec:
1. Direct pass on @bot, accurate reply-to-bot, or direct alias call (even without @).
2. Layer 1 Hard Rules: Detect @other_user (without @bot) -> silent; recalled message -> blocked_recall.
3. Layer 2 Semantics: Distinguish direct call vs. third-person mention about bot -> downweighted soft-entry.
4. Layer 3 Active Thread Tracking (Redis): Avoid interrupting active peer-to-peer chat between two group members.
5. Soft-entry downweight: Third-person banter or high-relevance open topic can short-reply if score >= threshold and air-guardrail passes.
6. Zero random probability! Strictly deterministic + semantic scoring.
7. Silent messages are NEVER discarded: should_record_ambient=True feeds L2 Ambient Memory.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

from app.core.redis_store import StateStore
from app.models.schemas import GatekeeperDecision, MessageEnvelope

logger = logging.getLogger(__name__)

# Patterns indicating third-person discussion ABOUT the bot rather than calling the bot directly
THIRD_PERSON_CUES = (
    "刚才",
    "笑死",
    "这家伙",
    "它",
    "他",
    "她",
    "是不是",
    "居然",
    "竟然",
    "好像",
    "天天",
    "每次",
    "真笨",
    "好蠢",
    "太强了",
)

# High-engagement / banter cues for open-topic or third-person soft entry
BANTER_OR_HOOK_CUES = (
    "笑死",
    "哈哈",
    "离谱",
    "刚才在聊啥",
    "刚才聊啥",
    "怎么看",
    "有人吗",
    "救命",
    "草",
    "蚌埠",
    "离大谱",
    "?",
    "？",
)


GOODBYE_CUES = ("没事了", "好了", "退下", "拜拜", "先忙了", "不用了", "闭嘴", "行了")


class GatekeeperService:
    def __init__(
        self,
        store: StateStore,
        soft_entry_threshold: float = 0.78,
        max_consecutive_turns: int = 4,
        attention_hot_seconds: float = 60.0,
        attention_decay_seconds: float = 240.0,
        max_attention_turns: int | None = None,
        group_topic_warmth_seconds: float = 180.0,
        group_topic_warmth_floor: float = 0.65,
    ) -> None:
        self.store = store
        self.soft_entry_threshold = soft_entry_threshold
        self.max_consecutive_turns = max_consecutive_turns
        self.attention_hot_seconds = attention_hot_seconds
        self.attention_decay_seconds = attention_decay_seconds
        self.max_attention_turns = max_attention_turns if max_attention_turns is not None else max(max_consecutive_turns * 2, 8)
        self.group_topic_warmth_seconds = group_topic_warmth_seconds
        self.group_topic_warmth_floor = group_topic_warmth_floor

    async def evaluate(
        self,
        envelope: MessageEnvelope,
        bot_name: str,
        aliases: list[str],
        replied_to_bot: bool = False,
        budget_fuse_active: bool = False,
        admin_qq: int = 0,
        llm_scorer: Callable[[MessageEnvelope, str], Awaitable[dict[str, Any] | None]] | None = None,
        soft_entry_threshold: float | None = None,
    ) -> GatekeeperDecision:
        # 0. Instant recall interception
        if await self.store.is_recalled(envelope.message_id):
            return GatekeeperDecision(
                action="blocked_recall",
                target="group_open",
                score=0.0,
                reason="Message was recalled before processing",
                should_record_ambient=False,
            )

        # Private DM always passes (unless budget fuse blocks non-admin? Spec: budget fuse keeps admin + explicit @)
        if envelope.scene == "private":
            if budget_fuse_active and admin_qq and envelope.sender_qq != admin_qq:
                return GatekeeperDecision(
                    action="silent",
                    target="bot",
                    score=0.2,
                    reason="Daily budget circuit breaker active (non-admin DM paused)",
                    should_record_ambient=True,
                )
            return GatekeeperDecision(
                action="direct_pass",
                target="bot",
                score=1.0,
                reason="Private DM direct conversation",
                should_record_ambient=False,
                suggested_max_chars=220,
            )

        group_id = envelope.group_id or 0
        text = (envelope.raw_text or "").strip()
        all_names = [n.strip() for n in [bot_name, *aliases] if n and n.strip()]
        has_media_image = any(
            isinstance(m, dict) and m.get("type") in ("image", "mface")
            for m in (envelope.media_meta or [])
        )
        has_topic_hook = bool(
            has_media_image
            or (envelope.reply_to_msg_id and not replied_to_bot)
            or any(c in text for c in ("?", "？", "吗", "啥", "哪个", "谁", "怎么"))
        )

        # --- Layer 1: Hard Rules (@, Reply, Peer Tracking) ---
        bot_mentioned = envelope.self_qq > 0 and envelope.self_qq in envelope.mentioned_qqs
        other_mentioned = [q for q in envelope.mentioned_qqs if q != envelope.self_qq]
        matched_alias = self._match_alias(text, all_names)
        is_direct_alias = bool(matched_alias and self._is_direct_call(text, matched_alias))

        # Record peer-to-peer interaction when user explicitly @s another group member (unless addressing bot)
        if other_mentioned and not bot_mentioned and not replied_to_bot and not is_direct_alias:
            await self.store.clear_user_attention(group_id, envelope.sender_qq)
            for peer_qq in other_mentioned:
                await self.store.record_peer_interaction(group_id, envelope.sender_qq, peer_qq)
            await self.store.record_group_human_message(group_id, envelope.sender_qq, has_hook=True)
            return GatekeeperDecision(
                action="silent",
                target="other_user",
                score=0.05,
                reason="Hard rule: message explicitly @mentions another member, not bot",
                should_record_ambient=True,
            )

        # Explicit @bot or accurate reply-to-bot -> High-priority direct pass
        if bot_mentioned or replied_to_bot:
            await self.store.record_user_attention(group_id, envelope.sender_qq, int(self.attention_decay_seconds) + 20)
            await self.store.record_group_bot_activity(group_id, ttl_seconds=max(180, int(self.group_topic_warmth_seconds) + 30))
            await self.store.record_group_human_message(group_id, envelope.sender_qq, has_hook=False)
            await self.store.record_bot_turn(group_id, reset=True)
            return GatekeeperDecision(
                action="direct_pass",
                target="bot",
                score=1.0,
                reason="Explicit @mention or reply to bot",
                should_record_ambient=True,
                suggested_max_chars=180,
            )

        # Budget 100% fuse: only allow admin or explicit @ (already handled above)
        if budget_fuse_active:
            if admin_qq and envelope.sender_qq == admin_qq:
                return GatekeeperDecision(
                    action="direct_pass",
                    target="bot",
                    score=0.95,
                    reason="Budget fuse active: admin override allowed",
                    should_record_ambient=True,
                )
            await self.store.record_group_human_message(group_id, envelope.sender_qq, has_hook=has_topic_hook)
            return GatekeeperDecision(
                action="silent",
                target="group_open",
                score=0.0,
                reason="Daily budget circuit breaker active: proactive group entry paused",
                should_record_ambient=True,
            )

        # --- Layer 2: Direct Alias Call vs. Third-Person Mention ---
        if matched_alias:
            if is_direct_alias:
                await self.store.record_user_attention(group_id, envelope.sender_qq, int(self.attention_decay_seconds) + 20)
                await self.store.record_group_bot_activity(group_id, ttl_seconds=max(180, int(self.group_topic_warmth_seconds) + 30))
                await self.store.record_group_human_message(group_id, envelope.sender_qq, has_hook=False)
                await self.store.record_bot_turn(group_id, reset=True)
                return GatekeeperDecision(
                    action="direct_pass",
                    target="bot",
                    score=0.95,
                    reason=f"Direct alias call without @: '{matched_alias}'",
                    should_record_ambient=True,
                    suggested_max_chars=160,
                )
            # Third-person mention about bot -> downweighted soft-entry candidate
            await self.store.record_group_human_message(group_id, envelope.sender_qq, has_hook=has_topic_hook)
            return await self._evaluate_soft_entry(
                envelope=envelope,
                target="third_person_about_bot",
                base_score=0.72,
                llm_scorer=llm_scorer,
                default_threshold=soft_entry_threshold,
            )

        # --- Layer 3: Active Peer-to-Peer Thread Guard & Air Guardrail ---
        active_peer = await self.store.get_active_peer(group_id, envelope.sender_qq)
        if active_peer and active_peer != envelope.self_qq:
            await self.store.record_group_human_message(group_id, envelope.sender_qq, has_hook=True)
            return GatekeeperDecision(
                action="silent",
                target="other_user",
                score=0.12,
                reason=f"Active peer-to-peer thread detected with QQ {active_peer}; staying silent",
                should_record_ambient=True,
            )

        # Time-Decaying Per-User Attention Window (0-60s hot -> 60-240s linear decay)
        # Check whether a third party interrupted after this user's last bot interaction
        last_human = await self.store.get_last_group_human_message(group_id)
        attention_age = await self.store.get_user_attention_age_seconds(group_id, envelope.sender_qq)
        attention_meta = await self.store.get_user_attention_meta(group_id, envelope.sender_qq)
        await self.store.record_group_human_message(group_id, envelope.sender_qq, has_hook=has_topic_hook)

        if attention_age is not None and attention_age <= self.attention_decay_seconds:
            # Explicit goodbye or replying to someone else clears attention immediately
            if any(cue in text for cue in GOODBYE_CUES) or (envelope.reply_to_msg_id and not replied_to_bot):
                await self.store.clear_user_attention(group_id, envelope.sender_qq)
            else:
                base_thr = self.soft_entry_threshold if soft_entry_threshold is None else soft_entry_threshold
                eff_threshold = self.compute_attention_threshold(attention_age, soft_entry_threshold=soft_entry_threshold)
                base = 0.72 if attention_age <= self.attention_hot_seconds else 0.50
                interrupted_by_other = bool(
                    last_human
                    and int(last_human.get("qq", 0)) != envelope.sender_qq
                    and attention_meta is not None
                    and (
                        int(last_human.get("seq", 0)) > int(attention_meta.get("seq", 0))
                        or float(last_human.get("ts", 0.0)) > float(attention_meta.get("ts", 0.0))
                    )
                )
                if interrupted_by_other:
                    if last_human.get("has_hook"):
                        # Third party asked a question, sent an image, or replied to someone -> revoke hot discount
                        eff_threshold = base_thr
                        base = 0.35
                    else:
                        eff_threshold = min(base_thr, round(eff_threshold + 0.25, 3))
                        base = 0.45

                decision = await self._evaluate_soft_entry(
                    envelope=envelope,
                    target="bot" if (attention_age <= self.attention_hot_seconds and not interrupted_by_other) else "group_open",
                    base_score=base,
                    llm_scorer=llm_scorer,
                    threshold_override=eff_threshold,
                    default_threshold=soft_entry_threshold,
                    attention_age_seconds=attention_age,
                    in_attention_window=True,
                    interrupted_by_other=interrupted_by_other,
                )
                if decision.action in ("direct_pass", "soft_entry"):
                    await self.store.record_user_attention(group_id, envelope.sender_qq, int(self.attention_decay_seconds) + 20)
                return decision

        # Group-level topic warmth window: when bot recently spoke in group, let other members riff on the topic
        group_warmth_age: float | None = None
        warmth_threshold: float | None = None
        if not (envelope.reply_to_msg_id and not replied_to_bot):
            raw_group_age = await self.store.get_group_bot_activity_age_seconds(group_id)
            if raw_group_age is not None and raw_group_age <= self.group_topic_warmth_seconds:
                group_warmth_age = raw_group_age
                warmth_threshold = self.compute_group_warmth_threshold(raw_group_age, soft_entry_threshold=soft_entry_threshold)

        # Open group message -> evaluate soft-entry with strict air-guardrail
        return await self._evaluate_soft_entry(
            envelope=envelope,
            target="group_open",
            base_score=0.35,
            llm_scorer=llm_scorer,
            threshold_override=warmth_threshold,
            default_threshold=soft_entry_threshold,
            group_warmth_age_seconds=group_warmth_age,
        )

    def compute_attention_threshold(
        self,
        age_seconds: float,
        soft_entry_threshold: float | None = None,
    ) -> float:
        """0..60s -> 0.35 (hot); 60..240s -> linear decay from 0.35 back to soft_entry_threshold (0.78)."""
        base_thr = self.soft_entry_threshold if soft_entry_threshold is None else soft_entry_threshold
        hot_floor = 0.35
        if age_seconds <= self.attention_hot_seconds:
            return hot_floor
        if age_seconds >= self.attention_decay_seconds:
            return base_thr
        span = max(1.0, self.attention_decay_seconds - self.attention_hot_seconds)
        progress = (age_seconds - self.attention_hot_seconds) / span
        return round(hot_floor + progress * (base_thr - hot_floor), 3)

    def compute_group_warmth_threshold(
        self,
        age_seconds: float,
        soft_entry_threshold: float | None = None,
    ) -> float:
        """0..group_topic_warmth_seconds -> linear decay from warmth_floor (0.65) back to soft_entry_threshold (0.78)."""
        base_thr = self.soft_entry_threshold if soft_entry_threshold is None else soft_entry_threshold
        if age_seconds <= 0.0:
            return self.group_topic_warmth_floor
        if age_seconds >= self.group_topic_warmth_seconds:
            return base_thr
        span = max(1.0, self.group_topic_warmth_seconds)
        progress = age_seconds / span
        return round(self.group_topic_warmth_floor + progress * (base_thr - self.group_topic_warmth_floor), 3)

    async def _evaluate_soft_entry(
        self,
        envelope: MessageEnvelope,
        target: str,
        base_score: float,
        llm_scorer: Callable[[MessageEnvelope, str], Awaitable[dict[str, Any] | None]] | None = None,
        threshold_override: float | None = None,
        default_threshold: float | None = None,
        attention_age_seconds: float | None = None,
        in_attention_window: bool = False,
        group_warmth_age_seconds: float | None = None,
        interrupted_by_other: bool = False,
    ) -> GatekeeperDecision:
        group_id = envelope.group_id or 0
        consecutive_turns = await self.store.get_consecutive_bot_turns(group_id)
        turn_cap = self.max_attention_turns if in_attention_window else self.max_consecutive_turns
        if consecutive_turns >= turn_cap:
            return GatekeeperDecision(
                action="silent",
                target=target,  # type: ignore[arg-type]
                score=0.1,
                reason=f"Air guardrail: reached max {'attention ' if in_attention_window else ''}consecutive turns ({turn_cap})",
                should_record_ambient=True,
            )

        score = base_score
        text = (envelope.raw_text or "").strip()

        # Heuristic semantic boost for banter / direct group question / teasing
        cue_hits = sum(1 for cue in BANTER_OR_HOOK_CUES if cue in text)
        if cue_hits:
            score = min(0.92, score + 0.12 * min(cue_hits, 2))

        # Optional lightweight Gatekeeper LLM scoring (structured JSON)
        reason = "Heuristic soft-entry evaluation"
        if llm_scorer is not None:
            try:
                llm_result = await llm_scorer(envelope, target)
                if llm_result and "score" in llm_result:
                    score = max(0.0, min(1.0, float(llm_result["score"])))
                    reason = str(llm_result.get("reason", "Gatekeeper LLM decision"))
                    if llm_result.get("target") == "other_user":
                        if in_attention_window:
                            await self.store.clear_user_attention(group_id, envelope.sender_qq)
                        return GatekeeperDecision(
                            action="silent",
                            target="other_user",
                            score=score,
                            reason=reason,
                            should_record_ambient=True,
                        )
                    if llm_result.get("target") == "bot" and target == "third_person_about_bot" and score >= 0.5:
                        await self.store.record_user_attention(group_id, envelope.sender_qq, int(self.attention_decay_seconds) + 20)
                        await self.store.record_group_bot_activity(group_id, ttl_seconds=max(180, int(self.group_topic_warmth_seconds) + 30))
                        await self.store.record_group_human_message(group_id, envelope.sender_qq, has_hook=False)
                        await self.store.record_bot_turn(group_id, reset=True)
                        return GatekeeperDecision(
                            action="direct_pass",
                            target="bot",
                            score=round(max(score, 0.85), 3),
                            reason=f"Direct alias call confirmed by Gatekeeper: {reason}",
                            should_record_ambient=True,
                            suggested_max_chars=160,
                        )
            except Exception as exc:
                logger.warning("Gatekeeper LLM scorer failed, using heuristic fallback [%s]: %s", type(exc).__name__, exc)

        base_thr = default_threshold if default_threshold is not None else self.soft_entry_threshold
        eff_thr = threshold_override if threshold_override is not None else base_thr
        if in_attention_window and consecutive_turns >= self.max_consecutive_turns:
            # Progressive tightening instead of hard cutoff: filters weak filler while keeping clear follow-ups alive
            extra_turns = consecutive_turns - self.max_consecutive_turns + 1
            eff_thr = min(0.90, round(eff_thr + 0.08 * extra_turns, 3))

        tags: list[str] = []
        if attention_age_seconds is not None:
            tags.append(f"attention={int(attention_age_seconds)}s")
        if interrupted_by_other:
            tags.append("interrupted")
        if group_warmth_age_seconds is not None:
            tags.append(f"warmth={int(group_warmth_age_seconds)}s")
        if in_attention_window and consecutive_turns >= self.max_consecutive_turns:
            tags.append(f"turn={consecutive_turns}")
        tag_suffix = f", {', '.join(tags)}" if tags else ""

        if score >= eff_thr:
            await self.store.record_bot_turn(group_id, reset=False)
            await self.store.record_group_bot_activity(group_id, ttl_seconds=max(180, int(self.group_topic_warmth_seconds) + 30))
            return GatekeeperDecision(
                action="soft_entry",
                target=target,  # type: ignore[arg-type]
                score=round(score, 3),
                reason=f"Soft-entry passed ({score:.2f} >= {eff_thr:.2f}{tag_suffix}): {reason}",
                should_record_ambient=True,
                suggested_max_chars=60,  # Soft-entry stays brief & punchy
            )

        return GatekeeperDecision(
            action="silent",
            target=target,  # type: ignore[arg-type]
            score=round(score, 3),
            reason=f"Downweighted silent ({score:.2f} < {eff_thr:.2f}{tag_suffix}); recorded to ambient memory",
            should_record_ambient=True,
        )

    @staticmethod
    def _match_alias(text: str, names: list[str]) -> str | None:
        lower = text.lower()
        for name in sorted(names, key=len, reverse=True):
            if name.lower() in lower:
                return name
        return None

    @staticmethod
    def _is_direct_call(text: str, alias: str) -> bool:
        """Distinguish direct addressing ('小呆呆，帮我看下') from 3rd-person gossip ('小呆呆刚才笑死我了')."""
        stripped = text.strip()
        if stripped == alias:
            return True
        has_second_person = "你" in stripped
        has_third_person_cue = any(cue in stripped for cue in THIRD_PERSON_CUES) and not has_second_person
        escaped = re.escape(alias)
        # Direct vocative at start with punctuation (e.g. '小呆呆，他怎么称呼' / '小呆呆: 刚才那个')
        if re.match(rf"^{escaped}\s*[,，、!！?？~～:：]", stripped):
            return True
        if re.match(rf"^{escaped}\s+", stripped):
            return not has_third_person_cue
        if re.match(rf"^[@＠]?(?:喂|诶|哎|哈喽|hello|hi)?[!！?？~～:：、\s,，]*{escaped}", stripped, re.IGNORECASE):
            return not has_third_person_cue
        # Ends with calling the bot ("...对吧小呆呆", "...在吗小呆呆")
        if re.search(rf"{escaped}[\s?？!！~～]*$", stripped) and not has_third_person_cue:
            return True
        return False
