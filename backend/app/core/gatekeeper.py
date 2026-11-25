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
import re
from typing import Any, Awaitable, Callable

from app.core.redis_store import StateStore
from app.models.schemas import GatekeeperDecision, MessageEnvelope

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


class GatekeeperService:
    def __init__(
        self,
        store: StateStore,
        soft_entry_threshold: float = 0.78,
        max_consecutive_turns: int = 4,
    ) -> None:
        self.store = store
        self.soft_entry_threshold = soft_entry_threshold
        self.max_consecutive_turns = max_consecutive_turns

    async def evaluate(
        self,
        envelope: MessageEnvelope,
        bot_name: str,
        aliases: list[str],
        replied_to_bot: bool = False,
        budget_fuse_active: bool = False,
        admin_qq: int = 0,
        llm_scorer: Callable[[MessageEnvelope, str], Awaitable[dict[str, Any] | None]] | None = None,
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

        # --- Layer 1: Hard Rules (@, Reply, Peer Tracking) ---
        bot_mentioned = envelope.self_qq > 0 and envelope.self_qq in envelope.mentioned_qqs
        other_mentioned = [q for q in envelope.mentioned_qqs if q != envelope.self_qq]

        # Record peer-to-peer interaction when user explicitly @s another group member
        if other_mentioned and not bot_mentioned:
            for peer_qq in other_mentioned:
                await self.store.record_peer_interaction(group_id, envelope.sender_qq, peer_qq)
            return GatekeeperDecision(
                action="silent",
                target="other_user",
                score=0.05,
                reason="Hard rule: message explicitly @mentions another member, not bot",
                should_record_ambient=True,
            )

        # Explicit @bot or accurate reply-to-bot -> High-priority direct pass
        if bot_mentioned or replied_to_bot:
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
            return GatekeeperDecision(
                action="silent",
                target="group_open",
                score=0.0,
                reason="Daily budget circuit breaker active: proactive group entry paused",
                should_record_ambient=True,
            )

        # --- Layer 2: Direct Alias Call vs. Third-Person Mention ---
        matched_alias = self._match_alias(text, all_names)
        if matched_alias:
            if self._is_direct_call(text, matched_alias):
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
            return await self._evaluate_soft_entry(
                envelope=envelope,
                target="third_person_about_bot",
                base_score=0.72,
                llm_scorer=llm_scorer,
            )

        # --- Layer 3: Active Peer-to-Peer Thread Guard & Air Guardrail ---
        active_peer = await self.store.get_active_peer(group_id, envelope.sender_qq)
        if active_peer and active_peer != envelope.self_qq:
            return GatekeeperDecision(
                action="silent",
                target="other_user",
                score=0.12,
                reason=f"Active peer-to-peer thread detected with QQ {active_peer}; staying silent",
                should_record_ambient=True,
            )

        # Open group message -> evaluate soft-entry with strict air-guardrail
        return await self._evaluate_soft_entry(
            envelope=envelope,
            target="group_open",
            base_score=0.35,
            llm_scorer=llm_scorer,
        )

    async def _evaluate_soft_entry(
        self,
        envelope: MessageEnvelope,
        target: str,
        base_score: float,
        llm_scorer: Callable[[MessageEnvelope, str], Awaitable[dict[str, Any] | None]] | None = None,
    ) -> GatekeeperDecision:
        group_id = envelope.group_id or 0
        consecutive_turns = await self.store.get_consecutive_bot_turns(group_id)
        if consecutive_turns >= self.max_consecutive_turns:
            return GatekeeperDecision(
                action="silent",
                target=target,  # type: ignore[arg-type]
                score=0.1,
                reason=f"Air guardrail: reached max consecutive turns ({self.max_consecutive_turns})",
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
                        return GatekeeperDecision(
                            action="silent",
                            target="other_user",
                            score=score,
                            reason=reason,
                            should_record_ambient=True,
                        )
            except Exception:
                pass  # Fall back cleanly to deterministic heuristic score

        if score >= self.soft_entry_threshold:
            await self.store.record_bot_turn(group_id, reset=False)
            return GatekeeperDecision(
                action="soft_entry",
                target=target,  # type: ignore[arg-type]
                score=round(score, 3),
                reason=f"Soft-entry passed ({score:.2f} >= {self.soft_entry_threshold:.2f}): {reason}",
                should_record_ambient=True,
                suggested_max_chars=60,  # Soft-entry stays brief & punchy
            )

        return GatekeeperDecision(
            action="silent",
            target=target,  # type: ignore[arg-type]
            score=round(score, 3),
            reason=f"Downweighted silent ({score:.2f} < {self.soft_entry_threshold:.2f}); recorded to ambient memory",
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
        # Check third-person cues first
        has_third_person_cue = any(cue in stripped for cue in THIRD_PERSON_CUES)
        # Direct vocative at start with punctuation or space or imperative/question
        escaped = re.escape(alias)
        if re.match(rf"^{escaped}[\s,，、!！?？~～:：]", stripped):
            return not has_third_person_cue
        if re.match(rf"^(喂|诶|哎|哈喽|hello|hi)?[\s,，]*{escaped}", stripped, re.IGNORECASE):
            if has_third_person_cue:
                return False
            return True
        # Ends with calling the bot ("...对吧小呆呆", "...在吗小呆呆")
        if re.search(rf"{escaped}[\s?？!！~～]*$", stripped) and not has_third_person_cue:
            return True
        return False
