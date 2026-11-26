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

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.multimodal_service import MultimodalService
from app.core.profile_schema import normalize_profile, relationship_tier
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

        participants = sorted({m.sender_qq for m in msgs if m.gatekeeper_decision != "bot_reply"})
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

    @staticmethod
    def _tokenize_for_recall(text: str) -> set[str]:
        """Extract word tokens and CJK 2-grams for lightweight language-agnostic lexical scoring."""
        clean = (text or "").strip().lower()
        if not clean:
            return set()
        tokens = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}", clean))
        cjk_runs = re.findall(r"[\u4e00-\u9fff]+", clean)
        for run in cjk_runs:
            if len(run) == 1:
                tokens.add(run)
            else:
                for i in range(len(run) - 1):
                    tokens.add(run[i : i + 2])
        return tokens

    @staticmethod
    def _cosine_similarity(vec_a: list[float] | None, vec_b: list[float] | None) -> float:
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(float(a) * float(b) for a, b in zip(vec_a, vec_b))
        na = math.sqrt(sum(float(a) * float(a) for a in vec_a))
        nb = math.sqrt(sum(float(b) * float(b) for b in vec_b))
        if na <= 1e-12 or nb <= 1e-12:
            return 0.0
        return dot / (na * nb)

    def _hybrid_rank_episodes(
        self,
        candidates: list[MemoryEpisode],
        query_text: str,
        query_embedding: list[float] | None,
        pg_fts_ranks: dict[int, float] | None,
        limit: int,
        pg_vec_sims: dict[int, float] | None = None,
    ) -> list[MemoryEpisode]:
        """Reciprocal Rank Fusion (RRF) across lexical match, vector similarity, importance, and recency."""
        if not candidates:
            return []
        q_tokens = self._tokenize_for_recall(query_text)
        now_utc = datetime.now(timezone.utc)

        scored: list[tuple[int, float, float, float, float]] = []
        for idx, ep in enumerate(candidates):
            doc_tokens = self._tokenize_for_recall(f"{ep.title} {ep.content}")
            overlap = (len(q_tokens & doc_tokens) / max(1, len(q_tokens))) if q_tokens else 0.0
            pg_bonus = (pg_fts_ranks or {}).get(ep.id, 0.0)
            lex = overlap + pg_bonus
            vec = (pg_vec_sims or {}).get(ep.id, 0.0)
            if vec <= 0.0 and query_embedding:
                vec = self._cosine_similarity(query_embedding, ep.embedding_json)
            imp = float(ep.importance or 0.5)
            ref_dt = ep.last_recalled_at or ep.created_at
            if ref_dt.tzinfo is None:
                ref_dt = ref_dt.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (now_utc - ref_dt).total_seconds() / 86400.0)
            recency = math.exp(-age_days / 30.0)
            scored.append((idx, lex, vec, imp, recency))

        rrf: dict[int, float] = {i: 0.0 for i in range(len(candidates))}
        k = 60.0

        # Lexical rank (only contributes when lexical hit > 0)
        lex_hits = [s for s in scored if s[1] > 0.0]
        for rank, (idx, *_) in enumerate(sorted(lex_hits, key=lambda x: x[1], reverse=True), start=1):
            rrf[idx] += 2.2 / (k + rank)

        # Vector similarity rank (only contributes when positive similarity)
        if query_embedding or pg_vec_sims:
            vec_hits = [s for s in scored if s[2] > 0.0]
            for rank, (idx, *_) in enumerate(sorted(vec_hits, key=lambda x: x[2], reverse=True), start=1):
                rrf[idx] += 2.0 / (k + rank)

        # Importance and recency prior
        for rank, (idx, *_) in enumerate(sorted(scored, key=lambda x: x[3], reverse=True), start=1):
            rrf[idx] += 1.0 / (k + rank)
        for rank, (idx, *_) in enumerate(sorted(scored, key=lambda x: x[4], reverse=True), start=1):
            rrf[idx] += 0.6 / (k + rank)

        ranked_indices = sorted(range(len(candidates)), key=lambda i: rrf[i], reverse=True)
        return [candidates[i] for i in ranked_indices[:limit]]

    async def reinforce_episodes(self, session: AsyncSession, episode_ids: list[int]) -> int:
        """Strengthen recalled L3 episodes when they contribute to positive social engagement."""
        unique_ids = [int(eid) for eid in dict.fromkeys(episode_ids or []) if eid]
        if not unique_ids:
            return 0
        now_utc = datetime.now(timezone.utc)
        rows = list((await session.execute(select(MemoryEpisode).where(MemoryEpisode.id.in_(unique_ids)))).scalars().all())
        for ep in rows:
            ep.recall_count = int(ep.recall_count or 0) + 1
            ep.last_recalled_at = now_utc
            ep.importance = min(1.0, round(float(ep.importance or 0.5) + 0.03, 4))
            ep.is_archived = False
        await session.flush()
        return len(rows)

    async def metabolize_episodes(
        self,
        session: AsyncSession,
        archive_after_days: int = 30,
        now_utc: datetime | None = None,
    ) -> dict[str, int]:
        """Decay unhit non-anniversary L3 episodes older than 30 days and archive low-importance ones (never delete)."""
        now = now_utc or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=max(1, archive_after_days))
        stmt = select(MemoryEpisode).where(
            MemoryEpisode.is_anniversary.is_(False),
            MemoryEpisode.is_archived.is_(False),
        )
        rows = list((await session.execute(stmt)).scalars().all())
        decayed = 0
        archived = 0
        for ep in rows:
            ref_dt = ep.last_recalled_at or ep.created_at
            if ref_dt.tzinfo is None:
                ref_dt = ref_dt.replace(tzinfo=timezone.utc)
            if ref_dt > cutoff:
                continue
            ep.importance = max(0.05, round(float(ep.importance or 0.5) * 0.75, 4))
            decayed += 1
            if ep.importance <= 0.35 or (int(ep.recall_count or 0) == 0 and ep.importance <= 0.50):
                ep.is_archived = True
                archived += 1
        await session.flush()
        return {"decayed": decayed, "archived": archived}

    async def assemble_context(
        self,
        session: AsyncSession,
        scene: str,
        sender_qq: int,
        group_id: int | None = None,
        working_limit: int = 16,
        query_text: str = "",
        query_embedding: list[float] | None = None,
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

        # L3 Episodic Memory (SQL-enforced semi-isolation + optional Hybrid FTS/Vector/RRF recall, excludes archived!)
        ep_limit = 8 if scene == "group" else 10
        iso_filter = (
            and_(
                MemoryEpisode.is_archived.is_(False),
                MemoryEpisode.scope == "public_group",
                or_(MemoryEpisode.group_id == group_id, MemoryEpisode.qq_id == sender_qq),
            )
            if scene == "group"
            else and_(
                MemoryEpisode.is_archived.is_(False),
                or_(
                    and_(MemoryEpisode.scope == "private_dm", MemoryEpisode.qq_id == sender_qq),
                    and_(MemoryEpisode.scope == "public_group", MemoryEpisode.qq_id == sender_qq),
                ),
            )
        )
        clean_query = (query_text or "").strip()
        recall_mode = "default"
        candidate_count = 0
        if clean_query or query_embedding:
            recall_mode = "hybrid"
            cand_stmt = (
                select(MemoryEpisode)
                .where(iso_filter)
                .order_by(MemoryEpisode.importance.desc(), MemoryEpisode.created_at.desc())
                .limit(50)
            )
            candidates = list((await session.execute(cand_stmt)).scalars().all())
            pg_fts_ranks: dict[int, float] = {}
            pg_vec_sims: dict[int, float] = {}
            bind = session.get_bind() if hasattr(session, "get_bind") else None
            dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
            if dialect_name == "postgresql" and clean_query and candidates:
                try:
                    fts_sql = select(
                        MemoryEpisode.id,
                        text("ts_rank_cd(tsv, websearch_to_tsquery('simple', :q)) AS fts_rank").bindparams(q=clean_query),
                    ).where(iso_filter, text("tsv @@ websearch_to_tsquery('simple', :q)").bindparams(q=clean_query))
                    for eid, rnk in (await session.execute(fts_sql)).all():
                        pg_fts_ranks[int(eid)] = float(rnk or 0.0)
                except Exception:
                    pg_fts_ranks = {}
            if dialect_name == "postgresql" and query_embedding and candidates:
                try:
                    vec_literal = "[" + ",".join(f"{float(v):.6f}" for v in query_embedding) + "]"
                    vec_sql = select(
                        MemoryEpisode.id,
                        text("1.0 - (embedding <=> CAST(:qv AS vector)) AS vec_sim").bindparams(qv=vec_literal),
                    ).where(iso_filter, text("embedding IS NOT NULL"))
                    for eid, sim in (await session.execute(vec_sql)).all():
                        if sim is not None:
                            pg_vec_sims[int(eid)] = float(sim)
                except Exception:
                    pg_vec_sims = {}
            candidate_count = len(candidates)
            episodes = self._hybrid_rank_episodes(
                candidates, clean_query, query_embedding, pg_fts_ranks, ep_limit, pg_vec_sims=pg_vec_sims
            )
        else:
            ep_stmt = (
                select(MemoryEpisode)
                .where(iso_filter)
                .order_by(MemoryEpisode.importance.desc(), MemoryEpisode.created_at.desc())
                .limit(ep_limit)
            )
            episodes = list((await session.execute(ep_stmt)).scalars().all())
            candidate_count = len(episodes)

        # L4 Acquaintance Card (Strict Semi-Isolation) & Approved Group Expressions
        identity = await session.get(QQIdentity, sender_qq)
        acquaintance_card: dict[str, Any] = {}
        if identity is not None:
            tier_key, tier_label = relationship_tier(identity.relationship_level)
            acquaintance_card = {
                "qq_id": identity.qq_id,
                "nickname": identity.nickname,
                "known_names": identity.known_names,
                "relationship_level": identity.relationship_level,
                "relationship_tier": tier_key,
                "relationship_tier_label": tier_label,
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

        url_desc_map = await MultimodalService.resolve_media_hints_map(
            session, [m.media_meta for m in l1_msgs]
        )

        return {
            "l1_working": [
                {
                    "sender_qq": m.sender_qq,
                    "sender": m.sender_display_name,
                    "text": m.raw_text,
                    "media_hint": MultimodalService.format_media_placeholder(m.media_meta, url_desc_map),
                    "reply_to": m.expanded_reply_text,
                    "is_ambient_only": m.is_ambient_only,
                    "is_bot": (m.gatekeeper_decision == "bot_reply"),
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
                    "id": ep.id,
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
            "l3_recall_meta": {
                "mode": recall_mode,
                "candidate_count": candidate_count,
                "recalled_count": len(episodes),
                "recalled_episode_ids": [ep.id for ep in episodes if ep.id is not None],
            },
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
