"""Five-Layer Deep Memory Service with Ambient Memory & SQL-Enforced Public/Private Semi-Isolation.

Layers:
- L1 Working Memory: recent verbatim turns + 30-min Group->DM context bridge.
- L2 Ambient Memory: silent/downweighted group messages stored & distilled into rolling 1-2h digests.
- L3 Episodic Memory: shared experiences & milestones (scoped to public_group / private_dm / self).
- L4 Acquaintance Cards & Expression Bank: public_profile vs. private_profile strictly isolated in group chats.
- L5 Self Life Memory: energy, mood, current activity, diary.
Plus: One-Click Forget (Blind Spot #4) cascading across messages, summaries, episodes, and profiles.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import (
    AmbientSummary,
    ChatMessage,
    GroupExpression,
    MemoryEpisode,
    QQIdentity,
    SelfLifeState,
)
from app.models.schemas import GatekeeperDecision, MessageEnvelope


class MemoryService:
    def __init__(self, dm_bridge_window_minutes: int = 30) -> None:
        self.dm_bridge_window_minutes = dm_bridge_window_minutes

    async def record_message(
        self,
        session: AsyncSession,
        envelope: MessageEnvelope,
        decision: GatekeeperDecision,
    ) -> ChatMessage:
        """Persist every message (both replied and ambient silent streams)."""
        msg = ChatMessage(
            message_id=envelope.message_id,
            scene=envelope.scene,
            group_id=envelope.group_id,
            sender_qq=envelope.sender_qq,
            sender_display_name=envelope.display_name,
            raw_text=envelope.raw_text,
            expanded_reply_text=envelope.expanded_reply_text,
            mentioned_qqs=envelope.mentioned_qqs,
            media_meta=envelope.media_meta,
            gatekeeper_decision=decision.action,
            gatekeeper_score=decision.score,
            is_recalled=(decision.action == "blocked_recall"),
            is_ambient_only=(decision.action == "silent"),
        )
        session.add(msg)
        await session.flush()
        return msg

    async def mark_message_recalled(self, session: AsyncSession, message_id: str) -> int:
        stmt = select(ChatMessage).where(ChatMessage.message_id == message_id)
        res = await session.execute(stmt)
        msgs = list(res.scalars().all())
        for m in msgs:
            m.is_recalled = True
        await session.flush()
        return len(msgs)

    async def build_ambient_summary(
        self,
        session: AsyncSession,
        group_id: int,
        hours: int = 2,
        summarizer: Any = None,
    ) -> AmbientSummary | None:
        """Distill recent silent/group messages into an L2 Ambient Rolling Summary."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=hours)
        stmt = (
            select(ChatMessage)
            .where(
                ChatMessage.scene == "group",
                ChatMessage.group_id == group_id,
                ChatMessage.is_recalled.is_(False),
                ChatMessage.created_at >= cutoff,
            )
            .order_by(ChatMessage.created_at.asc())
            .limit(80)
        )
        res = await session.execute(stmt)
        msgs = list(res.scalars().all())
        if not msgs:
            return None

        participants = sorted({m.sender_qq for m in msgs})
        lines = [f"{m.sender_display_name}: {m.raw_text}" for m in msgs if m.raw_text.strip()]
        if not lines:
            return None

        if summarizer is not None:
            summary_text = await summarizer(lines)
        else:
            # Deterministic lightweight extractive summary when no reflection LLM is configured
            snippet = " | ".join(lines[-8:])
            summary_text = f"近{hours}小时群内共 {len(msgs)} 条消息，活跃 {len(participants)} 人。近期讨论片段：{snippet[:420]}"

        summary = AmbientSummary(
            group_id=group_id,
            window_start=msgs[0].created_at,
            window_end=msgs[-1].created_at,
            summary_text=summary_text,
            active_participants=participants,
            topics=[],
            message_count=len(msgs),
        )
        session.add(summary)
        await session.flush()
        return summary

    async def assemble_context(
        self,
        session: AsyncSession,
        scene: str,
        sender_qq: int,
        group_id: int | None = None,
        working_limit: int = 16,
    ) -> dict[str, Any]:
        """Retrieve all 5 memory layers with STRICT SQL-level public/private semi-isolation.

        CRITICAL PRIVACY INVARIANT:
        - When scene == 'group': NEVER include sender's private_profile or any 'private_dm' episodes!
        - When scene == 'private': include both public_profile and private_profile, private_dm episodes,
          AND bridge recent group messages from this user within the last 30 minutes (L1 Group->DM bridge).
        """
        # L1 Working Memory
        if scene == "group":
            l1_stmt = (
                select(ChatMessage)
                .where(
                    ChatMessage.scene == "group",
                    ChatMessage.group_id == group_id,
                    ChatMessage.is_recalled.is_(False),
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(working_limit)
            )
            l1_msgs = list(reversed((await session.execute(l1_stmt)).scalars().all()))
            group_bridge_msgs: list[ChatMessage] = []
        else:
            l1_stmt = (
                select(ChatMessage)
                .where(
                    ChatMessage.scene == "private",
                    ChatMessage.sender_qq == sender_qq,
                    ChatMessage.is_recalled.is_(False),
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(working_limit)
            )
            l1_msgs = list(reversed((await session.execute(l1_stmt)).scalars().all()))

            # 30-minute Group -> DM context continuity bridge
            bridge_cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.dm_bridge_window_minutes)
            bridge_stmt = (
                select(ChatMessage)
                .where(
                    ChatMessage.scene == "group",
                    ChatMessage.sender_qq == sender_qq,
                    ChatMessage.is_recalled.is_(False),
                    ChatMessage.created_at >= bridge_cutoff,
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(6)
            )
            group_bridge_msgs = list(reversed((await session.execute(bridge_stmt)).scalars().all()))

        # L2 Ambient Rolling Summaries (group only)
        ambient_summaries: list[str] = []
        if scene == "group" and group_id is not None:
            amb_stmt = (
                select(AmbientSummary)
                .where(AmbientSummary.group_id == group_id)
                .order_by(AmbientSummary.window_end.desc())
                .limit(3)
            )
            ambient_summaries = [s.summary_text for s in (await session.execute(amb_stmt)).scalars().all()]

        # L3 Episodic Memory (SQL-enforced semi-isolation!)
        if scene == "group":
            ep_stmt = (
                select(MemoryEpisode)
                .where(
                    MemoryEpisode.scope == "public_group",
                    or_(MemoryEpisode.group_id == group_id, MemoryEpisode.qq_id == sender_qq),
                )
                .order_by(MemoryEpisode.importance.desc(), MemoryEpisode.created_at.desc())
                .limit(8)
            )
        else:
            ep_stmt = (
                select(MemoryEpisode)
                .where(
                    or_(
                        and_(MemoryEpisode.scope == "private_dm", MemoryEpisode.qq_id == sender_qq),
                        and_(MemoryEpisode.scope == "public_group", MemoryEpisode.qq_id == sender_qq),
                    )
                )
                .order_by(MemoryEpisode.importance.desc(), MemoryEpisode.created_at.desc())
                .limit(10)
            )
        episodes = list((await session.execute(ep_stmt)).scalars().all())

        # L4 Acquaintance Card (Strict Semi-Isolation) & Approved Group Expressions
        identity = await session.get(QQIdentity, sender_qq)
        acquaintance_card: dict[str, Any] = {}
        if identity is not None:
            acquaintance_card = {
                "qq_id": identity.qq_id,
                "nickname": identity.nickname,
                "known_names": identity.known_names,
                "relationship_level": identity.relationship_level,
                "public_profile": identity.public_profile,
            }
            if scene == "private":
                # ONLY expose private_profile in private DM!
                acquaintance_card["private_profile"] = identity.private_profile

        approved_slang: list[dict[str, str]] = []
        if scene == "group" and group_id is not None:
            slang_stmt = (
                select(GroupExpression)
                .where(GroupExpression.group_id == group_id, GroupExpression.status == "approved")
                .order_by(GroupExpression.usage_count.desc())
                .limit(12)
            )
            approved_slang = [
                {"phrase": e.phrase, "meaning": e.meaning}
                for e in (await session.execute(slang_stmt)).scalars().all()
            ]

        # L5 Self Life State
        life_stmt = select(SelfLifeState).order_by(SelfLifeState.recorded_at.desc()).limit(1)
        life_state = (await session.execute(life_stmt)).scalar_one_or_none()
        self_state = (
            {
                "energy_level": life_state.energy_level,
                "mood": life_state.mood,
                "current_activity": life_state.current_activity,
                "diary_entry": life_state.diary_entry,
            }
            if life_state
            else {"energy_level": 80, "mood": "relaxed", "current_activity": "摸鱼看群", "diary_entry": ""}
        )

        return {
            "l1_working": [
                {
                    "sender_qq": m.sender_qq,
                    "sender": m.sender_display_name,
                    "text": m.raw_text,
                    "reply_to": m.expanded_reply_text,
                    "is_ambient_only": m.is_ambient_only,
                }
                for m in l1_msgs
            ],
            "l1_group_to_dm_bridge": [
                {"group_id": m.group_id, "text": m.raw_text, "created_at": m.created_at.isoformat()}
                for m in group_bridge_msgs
            ],
            "l2_ambient_summaries": ambient_summaries,
            "l3_episodes": [
                {
                    "scope": ep.scope,
                    "title": ep.title,
                    "content": ep.content,
                    "is_anniversary": ep.is_anniversary,
                }
                for ep in episodes
            ],
            "l4_acquaintance": acquaintance_card,
            "l4_group_slang": approved_slang,
            "l5_self_state": self_state,
        }

    async def forget_user_data(
        self,
        session: AsyncSession,
        qq_id: int,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        wipe_profile: bool = True,
    ) -> dict[str, int]:
        """One-Click Forget (Blind Spot #4): cascade-delete raw messages, episodes, and clear profile fields."""
        msg_conds = [ChatMessage.sender_qq == qq_id]
        ep_conds = [MemoryEpisode.qq_id == qq_id]
        if start_time is not None:
            msg_conds.append(ChatMessage.created_at >= start_time)
            ep_conds.append(MemoryEpisode.created_at >= start_time)
        if end_time is not None:
            msg_conds.append(ChatMessage.created_at <= end_time)
            ep_conds.append(MemoryEpisode.created_at <= end_time)

        msg_res = await session.execute(delete(ChatMessage).where(and_(*msg_conds)))
        ep_res = await session.execute(delete(MemoryEpisode).where(and_(*ep_conds)))

        # Remove ambient summaries where this user participated in the affected window
        amb_stmt = select(AmbientSummary)
        amb_rows = list((await session.execute(amb_stmt)).scalars().all())
        amb_deleted = 0
        for row in amb_rows:
            if qq_id in (row.active_participants or []):
                in_range = True
                if start_time and row.window_end < start_time:
                    in_range = False
                if end_time and row.window_start > end_time:
                    in_range = False
                if in_range:
                    await session.delete(row)
                    amb_deleted += 1

        profile_wiped = 0
        if wipe_profile:
            identity = await session.get(QQIdentity, qq_id)
            if identity is not None:
                identity.public_profile = {}
                identity.private_profile = {}
                identity.relationship_level = 0
                profile_wiped = 1

        await session.flush()
        return {
            "messages_deleted": msg_res.rowcount or 0,
            "episodes_deleted": ep_res.rowcount or 0,
            "ambient_summaries_deleted": amb_deleted,
            "profiles_wiped": profile_wiped,
        }
