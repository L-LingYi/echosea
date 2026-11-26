"""Nightly Offline Reflection & Controlled Evolution Engine:
Distills L1/L2 streams into:
1. Dual-track 4D acquaintance profiles (group -> public_profile, private -> private_profile) with
   anti-joke candidate_clues confirmation, mandatory 7-14d recent_states TTL, and PII scrubbing.
2. Candidate group slang (GroupExpression status='pending').
3. Shared episodic memories & interaction anniversaries + 30-day L3 memory metabolism (decay + archive).
4. Implicit social feedback aggregation (120s cold_drop finalization, 7d guarded threshold tuning [0.68, 0.88],
   and high-praise pending Few-Shot candidate extraction).
5. Bot expression habit self-audit (recent 20 replies -> 48h TTL correction_hint + pending banned_phrases).
REDLINE: Never auto-rewrites PersonaVersion.system_prompt! Core personality suggestions stay pending for WebUI review.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import local_now
from app.core.memory_service import MemoryService
from app.core.model_hub import ModelHubService
from app.core.profile_schema import (
    normalize_profile,
    observe_profile_clue,
    observe_recent_state,
    relationship_tier,
    sanitize_clue_text,
)
from app.models.db import (
    ChatMessage,
    EvolutionSuggestion,
    GroupExpression,
    MemoryEpisode,
    OutboxDelivery,
    PersonaVersion,
    QQIdentity,
    SystemConfig,
)

logger = logging.getLogger(__name__)

SLANG_PATTERN = re.compile(r"「([^」]{2,10})」|“([^”]{2,10})”")

INTEREST_CUES = {
    "原神": "原神",
    "崩铁": "崩坏：星穹铁道",
    "鸣潮": "鸣潮",
    "绝区零": "绝区零",
    "写代码": "编程开发",
    "露营": "户外露营",
    "火锅": "火锅美食",
    "烧烤": "烧烤",
    "健身": "健身",
    "猫": "吸猫",
    "狗": "撸狗",
    "摄影": "摄影",
    "二次元": "二次元",
    "看番": "追番",
    "咖啡": "咖啡",
    "羽毛球": "羽毛球",
    "骑行": "骑行",
    "游戏": "打游戏",
}

EXPERTISE_CUES = {
    "python": "Python",
    "写后端": "后端开发",
    "写前端": "前端开发",
    "剪辑": "视频剪辑",
    "画画": "绘画",
    "做饭": "下厨",
    "修电脑": "折腾硬件",
}

SCHEDULE_CUES = {
    "熬夜": "夜猫子",
    "通宵": "夜猫子",
    "凌晨睡": "夜猫子",
    "早起": "早起党",
    "加班": "常加班",
}

RECENT_STATE_CUES = (
    ("赶项目", "最近在赶项目", 10),
    ("换设备", "刚换新设备", 10),
    ("换手机", "刚换新手机", 10),
    ("换电脑", "刚换新电脑", 10),
    ("找工作", "最近在看工作机会", 14),
    ("面试", "最近在准备面试", 10),
    ("出差", "近期在出差", 7),
    ("考试", "最近在备考", 10),
    ("感冒", "这两天有点感冒", 7),
    ("发烧", "这两天身体不太舒服", 7),
)

BOUNDARY_RE = re.compile(r"(?:别提|不要提|别聊|少提|讨厌聊)([\u4e00-\u9fffA-Za-z0-9]{2,12})")
ANNIVERSARY_CUES = ("周年", "纪念日", "生日", "第一次", "认识一")

# Threshold guardrail bounds
THRESHOLD_MIN = 0.68
THRESHOLD_MAX = 0.88
THRESHOLD_STEP = 0.02

OPENER_RE = re.compile(r"^[，,。！!？?\s~～]*([A-Za-z0-9]+|[\u4e00-\u9fff]{1,2})")
CLICHE_OPENER_CANDIDATES = {"确实", "不错", "可以", "接住", "记下", "当然", "没问题", "我觉得", "那确实", "哎呀", "哈哈", "哟"}


class ReflectionService:
    """Runs offline reflection using the 'reflection' model slot (or deterministic fallback extractor)."""

    def __init__(self, model_hub: ModelHubService, timezone_name: str = "Asia/Shanghai") -> None:
        self.model_hub = model_hub
        self.timezone_name = timezone_name
        self.memory_service = MemoryService()

    async def run_nightly_reflection(
        self,
        session: AsyncSession,
        group_id: int | None = None,
        lookback_hours: int = 24,
        now_utc: datetime | None = None,
    ) -> dict[str, Any]:
        now = now_utc or datetime.now(timezone.utc)
        today_local = local_now(self.timezone_name).date()
        today_str = today_local.isoformat()

        # 0. Finalize expired pending group feedback (>120s -> cold_drop)
        cold_drops_finalized = await self._finalize_cold_drops(session, now)

        # 1. Guarded 7-day group soft_entry_threshold tuning & high-praise Few-Shot extraction
        thresholds_tuned = await self._tune_group_thresholds(session, now, today_str, group_id_filter=group_id)
        few_shot_candidates = await self._extract_few_shot_candidates(session, now, group_id_filter=group_id)

        # 2. L3 memory metabolism (decay + archive >30d unhit ordinary episodes, never delete)
        metabolism = await self.memory_service.metabolize_episodes(session, archive_after_days=30, now_utc=now)

        # 3. Bot expression self-audit on recent 20 replies
        audit = await self._audit_bot_expression_habits(session, now)

        cutoff = now - timedelta(hours=lookback_hours)
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.is_recalled.is_(False), ChatMessage.created_at >= cutoff)
            .order_by(ChatMessage.created_at.asc())
            .limit(400)
        )
        if group_id is not None:
            stmt = stmt.where(ChatMessage.group_id == group_id)
        msgs = list((await session.execute(stmt)).scalars().all())
        if not msgs:
            await session.flush()
            return {
                "processed_messages": 0,
                "profiles_updated": 0,
                "slang_discovered": 0,
                "episodes_created": 0,
                "cold_drops_finalized": cold_drops_finalized,
                "thresholds_tuned": thresholds_tuned,
                "few_shot_candidates": few_shot_candidates,
                "episodes_decayed": metabolism["decayed"],
                "episodes_archived": metabolism["archived"],
                **audit,
            }

        # 4. Try reflection LLM slot if configured; otherwise run deterministic NLP extraction
        llm_extracted = await self._try_llm_reflection(session, msgs)

        # 5. Dual-track 4D acquaintance profiles (group -> public_profile, private -> private_profile)
        profiles_updated = await self._update_dual_track_profiles(session, msgs, llm_extracted, now)

        # 6. Discover candidate group slang (status='pending' for WebUI moderation)
        slang_discovered = await self._discover_group_slang(session, msgs, llm_extracted, group_id)

        # 7. Extract episodic milestones & interaction anniversaries
        episodes_created = await self._extract_episodes(session, msgs, llm_extracted, group_id, today_local)

        # 8. Record any LLM core persona advice strictly as pending suggestions (NEVER mutate system_prompt!)
        persona_advices_created = await self._record_persona_advices(session, llm_extracted)

        await session.flush()
        return {
            "processed_messages": len(msgs),
            "profiles_updated": profiles_updated,
            "slang_discovered": slang_discovered,
            "episodes_created": episodes_created,
            "cold_drops_finalized": cold_drops_finalized,
            "thresholds_tuned": thresholds_tuned,
            "few_shot_candidates": few_shot_candidates,
            "episodes_decayed": metabolism["decayed"],
            "episodes_archived": metabolism["archived"],
            "persona_advices_created": audit["persona_advices_created"] + persona_advices_created,
            "correction_hints_created": audit["correction_hints_created"],
            "banned_phrase_candidates": audit["banned_phrase_candidates"],
        }

    async def _finalize_cold_drops(self, session: AsyncSession, now_utc: datetime) -> int:
        cutoff = now_utc - timedelta(seconds=120)
        rows = list(
            (
                await session.execute(
                    select(OutboxDelivery).where(
                        OutboxDelivery.scene == "group",
                        OutboxDelivery.status == "sent",
                        OutboxDelivery.feedback_status == "pending",
                        OutboxDelivery.created_at <= cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.feedback_status = "cold_drop"
            row.feedback_updated_at = now_utc
        await session.flush()
        return len(rows)

    async def _tune_group_thresholds(
        self,
        session: AsyncSession,
        now_utc: datetime,
        today_str: str,
        group_id_filter: int | None = None,
    ) -> int:
        since = now_utc - timedelta(days=7)
        stmt = select(OutboxDelivery).where(
            OutboxDelivery.scene == "group",
            OutboxDelivery.status == "sent",
            OutboxDelivery.created_at >= since,
            OutboxDelivery.feedback_status.in_(("engaged", "cold_drop", "rejected")),
        )
        if group_id_filter is not None:
            stmt = stmt.where(OutboxDelivery.target_id == group_id_filter)
        rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            return 0

        by_group: dict[int, dict[str, int]] = {}
        seen_requests: set[tuple[int, str]] = set()
        for r in rows:
            gid = int(r.target_id)
            req_key = (gid, r.request_id or f"id_{r.id}")
            if req_key in seen_requests:
                continue
            seen_requests.add(req_key)
            stats = by_group.setdefault(gid, {"engaged": 0, "cold_drop": 0, "rejected": 0})
            stats[r.feedback_status] = stats.get(r.feedback_status, 0) + 1

        tuned = 0
        for gid, stats in by_group.items():
            total = stats["engaged"] + stats["cold_drop"] + stats["rejected"]
            if total < 5:
                continue
            success_rate = stats["engaged"] / total
            reject_rate = stats["rejected"] / total

            cfg_stmt = select(SystemConfig).where(
                SystemConfig.scope_type == "group",
                SystemConfig.scope_id == str(gid),
            )
            cfg_row = (await session.execute(cfg_stmt)).scalar_one_or_none()
            cfg = dict(cfg_row.config_json or {}) if cfg_row else {}
            if cfg.get("last_threshold_tuned_date") == today_str:
                continue

            cur_thr = float(cfg.get("soft_entry_threshold", 0.78))
            delta = 0.0
            if success_rate >= 0.78 and reject_rate <= 0.05:
                delta = -THRESHOLD_STEP
            elif success_rate < 0.55 or reject_rate >= 0.15:
                delta = THRESHOLD_STEP

            if delta == 0.0:
                continue
            new_thr = max(THRESHOLD_MIN, min(THRESHOLD_MAX, round(cur_thr + delta, 2)))
            cfg["soft_entry_threshold"] = new_thr
            cfg["last_threshold_tuned_date"] = today_str
            cfg["last_7d_feedback"] = {**stats, "total": total, "success_rate": round(success_rate, 3)}
            if cfg_row is None:
                session.add(SystemConfig(scope_type="group", scope_id=str(gid), config_json=cfg))
            else:
                cfg_row.config_json = cfg
            tuned += 1

        await session.flush()
        return tuned

    async def _extract_few_shot_candidates(
        self,
        session: AsyncSession,
        now_utc: datetime,
        group_id_filter: int | None = None,
    ) -> int:
        since = now_utc - timedelta(days=3)
        stmt = (
            select(OutboxDelivery)
            .where(
                OutboxDelivery.scene == "group",
                OutboxDelivery.status == "sent",
                OutboxDelivery.feedback_status == "engaged",
                OutboxDelivery.created_at >= since,
                OutboxDelivery.trigger_msg_id.is_not(None),
            )
            .order_by(OutboxDelivery.created_at.desc())
            .limit(20)
        )
        if group_id_filter is not None:
            stmt = stmt.where(OutboxDelivery.target_id == group_id_filter)
        engaged_rows = list((await session.execute(stmt)).scalars().all())
        if not engaged_rows:
            return 0

        active_persona = (
            await session.execute(select(PersonaVersion).where(PersonaVersion.is_active.is_(True)).limit(1))
        ).scalar_one_or_none()
        existing_pairs = {
            (str(ex.get("user", "")).strip(), str(ex.get("assistant", "")).strip())
            for ex in (active_persona.few_shot_examples if active_persona else [])
            if isinstance(ex, dict)
        }
        existing_sugs = list(
            (
                await session.execute(
                    select(EvolutionSuggestion).where(EvolutionSuggestion.suggestion_type == "few_shot")
                )
            )
            .scalars()
            .all()
        )
        for s in existing_sugs:
            cj = s.content_json or {}
            existing_pairs.add((str(cj.get("user", "")).strip(), str(cj.get("assistant", "")).strip()))

        created = 0
        seen_req: set[str] = set()
        for row in engaged_rows:
            if created >= 5:
                break
            req_id = row.request_id or f"out_{row.id}"
            if req_id in seen_req:
                continue
            seen_req.add(req_id)
            trig = (
                await session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.message_id == row.trigger_msg_id, ChatMessage.is_recalled.is_(False))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if trig is None:
                continue
            u_text = (trig.raw_text or "").strip()
            a_text = (row.payload_text or "").strip()
            if not (4 <= len(u_text) <= 80 and 2 <= len(a_text) <= 80):
                continue
            if (u_text, a_text) in existing_pairs:
                continue
            existing_pairs.add((u_text, a_text))
            session.add(
                EvolutionSuggestion(
                    suggestion_type="few_shot",
                    status="pending",
                    group_id=int(row.target_id),
                    source_request_id=row.request_id,
                    title=f"高赞回复候选：{a_text[:24]}",
                    content_json={"user": u_text, "assistant": a_text},
                    reason="群内 120 秒内获得正向接茬/互动反馈，建议审核加入 Few-Shot 示例。",
                )
            )
            created += 1

        await session.flush()
        return created

    async def _update_dual_track_profiles(
        self,
        session: AsyncSession,
        msgs: list[ChatMessage],
        llm_extracted: dict[str, Any],
        now_utc: datetime,
    ) -> int:
        # Track: (qq_id, "public" | "private") -> list of actions/clues
        ident_cache: dict[int, QQIdentity] = {}
        dirty_idents: set[int] = set()

        async def _get_ident(qq: int) -> QQIdentity | None:
            if qq in ident_cache:
                return ident_cache[qq]
            row = await session.get(QQIdentity, qq)
            if row is not None:
                ident_cache[qq] = row
            return row

        # Ensure bot's own QQ isn't profiled as a group member if present
        profiles_by_track: dict[tuple[int, str], dict[str, Any]] = {}

        async def _get_track_profile(qq: int, track: str) -> tuple[QQIdentity | None, dict[str, Any] | None]:
            ident = await _get_ident(qq)
            if ident is None:
                return None, None
            key = (qq, track)
            if key not in profiles_by_track:
                raw = ident.public_profile if track == "public" else ident.private_profile
                profiles_by_track[key] = normalize_profile(
                    raw, relationship_level=ident.relationship_level, now_utc=now_utc
                )
            return ident, profiles_by_track[key]

        bot_qqs = {int(m.sender_qq) for m in msgs if m.gatekeeper_decision == "bot_reply" and m.sender_qq}
        for m in msgs:
            if m.gatekeeper_decision == "bot_reply" or m.sender_qq in bot_qqs:
                continue
            track = "public" if m.scene == "group" else "private"
            strict_public = track == "public"
            ident, prof = await _get_track_profile(m.sender_qq, track)
            if ident is None or prof is None:
                continue

            msg_date = (m.created_at or now_utc).date().isoformat()
            raw_text = (m.raw_text or "").strip()
            if not raw_text:
                continue

            changed = False
            for kw, label in INTEREST_CUES.items():
                if kw in raw_text:
                    changed |= observe_profile_clue(prof, "interests", label, msg_date, now_utc, strict_public=strict_public)
            for kw, label in EXPERTISE_CUES.items():
                if kw.lower() in raw_text.lower():
                    changed |= observe_profile_clue(prof, "expertise", label, msg_date, now_utc, strict_public=strict_public)
            for kw, label in SCHEDULE_CUES.items():
                if kw in raw_text:
                    changed |= observe_profile_clue(
                        prof, "schedule_habits", label, msg_date, now_utc, strict_public=strict_public
                    )
            for kw, state_text, ttl_days in RECENT_STATE_CUES:
                if kw in raw_text:
                    changed |= observe_recent_state(prof, state_text, now_utc, ttl_days=ttl_days, strict_public=strict_public)
            b_match = BOUNDARY_RE.search(raw_text)
            if b_match:
                b_val = sanitize_clue_text(b_match.group(1), max_len=20, strict_public=strict_public)
                if b_val and b_val not in prof["relationship"]["boundaries"]:
                    prof["relationship"]["boundaries"].append(b_val)
                    prof["relationship"]["boundaries"] = prof["relationship"]["boundaries"][:8]
                    changed = True

            if changed:
                dirty_idents.add(m.sender_qq)

        # Merge LLM extracted clues respecting public/private track isolation
        today_str = now_utc.date().isoformat()
        for item in llm_extracted.get("profile_clues", []):
            if not isinstance(item, dict):
                continue
            qq = int(item.get("qq_id") or 0)
            if not qq or qq in bot_qqs:
                continue
            scene_val = str(item.get("scene") or "group")
            track = "private" if scene_val == "private" else "public"
            strict_public = track == "public"
            ident, prof = await _get_track_profile(qq, track)
            if ident is None or prof is None:
                continue

            changed = False
            category = str(item.get("category") or "interests")
            val = str(item.get("value") or item.get("interest") or "").strip()
            if val:
                changed |= observe_profile_clue(prof, category, val, today_str, now_utc, strict_public=strict_public)
            r_state = str(item.get("recent_state") or "").strip()
            if r_state:
                ttl_days = int(item.get("ttl_days") or 10)
                changed |= observe_recent_state(prof, r_state, now_utc, ttl_days=ttl_days, strict_public=strict_public)
            style = sanitize_clue_text(str(item.get("interaction_style") or ""), max_len=20, strict_public=strict_public)
            if style and prof["relationship"]["interaction_style"] != style:
                prof["relationship"]["interaction_style"] = style
                changed = True
            addr = sanitize_clue_text(str(item.get("preferred_address") or ""), max_len=16, strict_public=strict_public)
            if addr and prof["relationship"]["preferred_address"] != addr:
                prof["relationship"]["preferred_address"] = addr
                changed = True
            boundary = sanitize_clue_text(str(item.get("boundary") or ""), max_len=24, strict_public=strict_public)
            if boundary and boundary not in prof["relationship"]["boundaries"]:
                prof["relationship"]["boundaries"].append(boundary)
                prof["relationship"]["boundaries"] = prof["relationship"]["boundaries"][:8]
                changed = True
            if changed:
                dirty_idents.add(qq)

        # Persist normalized profiles (and prune expired states)
        updated_count = 0
        for (qq, track), prof in profiles_by_track.items():
            ident = ident_cache[qq]
            tier_key, _ = relationship_tier(ident.relationship_level)
            prof["relationship"]["familiarity_tier"] = tier_key
            old_raw = ident.public_profile if track == "public" else ident.private_profile
            if qq in dirty_idents or prof != (old_raw or {}):
                if track == "public":
                    ident.public_profile = prof
                else:
                    ident.private_profile = prof
                if qq in dirty_idents:
                    updated_count += 1

        # Count unique identities meaningfully updated
        await session.flush()
        return len(dirty_idents) if dirty_idents else updated_count

    async def _discover_group_slang(
        self,
        session: AsyncSession,
        msgs: list[ChatMessage],
        llm_extracted: dict[str, Any],
        group_id: int | None,
    ) -> int:
        slang_counter: Counter[tuple[int, str]] = Counter()
        for m in msgs:
            if m.scene == "group" and m.group_id and m.gatekeeper_decision != "bot_reply":
                for match in SLANG_PATTERN.finditer(m.raw_text):
                    phrase = (match.group(1) or match.group(2) or "").strip()
                    if phrase:
                        slang_counter[(m.group_id, phrase)] += 1
        for item in llm_extracted.get("slang_candidates", []):
            gid = int(item.get("group_id") or group_id or 0)
            phrase = str(item.get("phrase", "")).strip()
            if gid and phrase:
                slang_counter[(gid, phrase)] += 2

        slang_discovered = 0
        for (gid, phrase), count in slang_counter.items():
            if count < 2:
                continue
            ex_stmt = select(GroupExpression).where(GroupExpression.group_id == gid, GroupExpression.phrase == phrase)
            ex = (await session.execute(ex_stmt)).scalar_one_or_none()
            if ex is None:
                session.add(
                    GroupExpression(
                        group_id=gid,
                        phrase=phrase,
                        meaning="离线反思自动发现（待审核）",
                        status="pending",
                        usage_count=count,
                    )
                )
                slang_discovered += 1
            else:
                ex.usage_count += count
        return slang_discovered

    async def _extract_episodes(
        self,
        session: AsyncSession,
        msgs: list[ChatMessage],
        llm_extracted: dict[str, Any],
        group_id: int | None,
        today_local: Any,
    ) -> int:
        episodes_created = 0
        bot_qqs = {int(m.sender_qq) for m in msgs if m.gatekeeper_decision == "bot_reply" and m.sender_qq}
        for m in msgs:
            if m.gatekeeper_decision == "bot_reply" or m.sender_qq in bot_qqs:
                continue
            if any(cue in m.raw_text for cue in ANNIVERSARY_CUES):
                scope = "public_group" if m.scene == "group" else "private_dm"
                session.add(
                    MemoryEpisode(
                        scope=scope,
                        group_id=m.group_id,
                        qq_id=m.sender_qq,
                        title="互动纪念时刻",
                        content=f"{m.sender_display_name}: {m.raw_text[:200]}",
                        importance=0.85,
                        is_anniversary=True,
                        event_date=today_local,
                    )
                )
                episodes_created += 1

        for ep in llm_extracted.get("episodes", []):
            content = str(ep.get("content", "")).strip()
            if not content:
                continue
            scope = "private_dm" if ep.get("scope") == "private_dm" else "public_group"
            session.add(
                MemoryEpisode(
                    scope=scope,
                    group_id=None if scope == "private_dm" else (ep.get("group_id") or group_id),
                    qq_id=ep.get("qq_id"),
                    title=str(ep.get("title", "共同经历"))[:200],
                    content=content,
                    importance=float(ep.get("importance", 0.7)),
                    is_anniversary=bool(ep.get("is_anniversary", False)),
                    event_date=today_local,
                )
            )
            episodes_created += 1
        return episodes_created

    async def _audit_bot_expression_habits(self, session: AsyncSession, now_utc: datetime) -> dict[str, int]:
        """Sample recent 20 bot replies for verbosity & repetitive openers; emit 48h hints & pending banned_phrases."""
        # Expire stale hints first
        active_hints = list(
            (
                await session.execute(
                    select(EvolutionSuggestion).where(
                        EvolutionSuggestion.suggestion_type == "correction_hint",
                        EvolutionSuggestion.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        for h in active_hints:
            exp = h.expires_at
            if exp is not None:
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp <= now_utc:
                    h.status = "expired"

        replies = [
            r.strip()
            for r in (
                await session.execute(
                    select(ChatMessage.raw_text)
                    .where(ChatMessage.gatekeeper_decision == "bot_reply", ChatMessage.is_recalled.is_(False))
                    .order_by(ChatMessage.id.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
            if r and r.strip()
        ]
        if len(replies) < 5:
            replies = [
                r.strip()
                for r in (
                    await session.execute(
                        select(OutboxDelivery.payload_text)
                        .where(OutboxDelivery.status == "sent")
                        .order_by(OutboxDelivery.id.desc())
                        .limit(20)
                    )
                )
                .scalars()
                .all()
                if r and r.strip()
            ]

        if len(replies) < 5:
            return {"correction_hints_created": 0, "banned_phrase_candidates": 0, "persona_advices_created": 0}

        hint_parts: list[str] = []
        avg_len = sum(len(r) for r in replies) / len(replies)
        if avg_len > 45:
            hint_parts.append(f"近期回复平均偏长（约 {int(round(avg_len))} 字），接下来群聊尽量压到 10~25 字短句。")

        opener_counts: Counter[str] = Counter()
        for r in replies:
            m = OPENER_RE.match(r)
            if m:
                opener_counts[m.group(1)] += 1

        banned_created = 0
        if opener_counts:
            top_opener, top_cnt = opener_counts.most_common(1)[0]
            if top_cnt >= 3 and (top_cnt / len(replies)) >= 0.30:
                hint_parts.append(f"最近 {top_cnt}/{len(replies)} 条回复都以「{top_opener}」开头，请换不同起手式，防止复读模板味。")
                if top_opener in CLICHE_OPENER_CANDIDATES or (len(top_opener) >= 2 and (top_cnt / len(replies)) >= 0.45):
                    active_persona = (
                        await session.execute(select(PersonaVersion).where(PersonaVersion.is_active.is_(True)).limit(1))
                    ).scalar_one_or_none()
                    existing_banned = set(active_persona.banned_phrases or []) if active_persona else set()
                    dup_sug = (
                        await session.execute(
                            select(EvolutionSuggestion)
                            .where(
                                EvolutionSuggestion.suggestion_type == "banned_phrase",
                                EvolutionSuggestion.title == f"高频口癖词：{top_opener}",
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if top_opener not in existing_banned and dup_sug is None:
                        session.add(
                            EvolutionSuggestion(
                                suggestion_type="banned_phrase",
                                status="pending",
                                title=f"高频口癖词：{top_opener}",
                                content_json={"phrase": top_opener, "count": top_cnt, "sample_size": len(replies)},
                                reason=f"近 {len(replies)} 条回复中有 {top_cnt} 条以「{top_opener}」起手，建议审核心意加入禁用词。",
                            )
                        )
                        banned_created += 1

        hints_created = 0
        if hint_parts:
            combined_hint = " ".join(hint_parts)
            # Replace older active auto-hints so prompt stays clean and bounded
            for h in active_hints:
                if h.status == "active":
                    h.status = "expired"
            session.add(
                EvolutionSuggestion(
                    suggestion_type="correction_hint",
                    status="active",
                    title="48h 表达自纠偏提醒",
                    content_json={"hint": combined_hint, "avg_len": round(avg_len, 1), "sample_size": len(replies)},
                    reason="夜间抽检最近 20 条回复自动生成，48 小时后自动失效（不修改核心人设）。",
                    expires_at=now_utc + timedelta(hours=48),
                )
            )
            hints_created = 1

        await session.flush()
        return {
            "correction_hints_created": hints_created,
            "banned_phrase_candidates": banned_created,
            "persona_advices_created": 0,
        }

    async def _record_persona_advices(self, session: AsyncSession, llm_extracted: dict[str, Any]) -> int:
        created = 0
        for adv in llm_extracted.get("persona_advices", []):
            if not isinstance(adv, dict):
                continue
            advice_text = str(adv.get("advice") or adv.get("suggestion") or "").strip()
            if not advice_text:
                continue
            session.add(
                EvolutionSuggestion(
                    suggestion_type="persona_advice",
                    status="pending",
                    title=str(adv.get("title") or "人设微调建议（待人工审批）")[:180],
                    content_json={"advice": advice_text},
                    reason=str(adv.get("reason") or "夜间反思分析提出的核心性格建议，绝不自动覆盖 System Prompt。"),
                )
            )
            created += 1
        return created

    async def _try_llm_reflection(self, session: AsyncSession, msgs: list[ChatMessage]) -> dict[str, Any]:
        transcript = "\n".join(
            f"[{m.scene}][{m.group_id or 'DM'}][{m.sender_qq}]{m.sender_display_name}: {m.raw_text}"
            for m in msgs[-60:]
            if m.gatekeeper_decision != "bot_reply"
        )
        if not transcript.strip():
            return {}
        prompt = (
            "请从以下聊天记录提炼 JSON（注意区分 group 公开与 private 私聊，严禁把手机号/住址/工资等隐私写入公开线索）：\n"
            '{"profile_clues":[{"qq_id":123,"scene":"group","category":"interests","value":"...","recent_state":"","ttl_days":10,"interaction_style":"","boundary":""}],'
            '"slang_candidates":[{"group_id":123,"phrase":"..."}],'
            '"episodes":[{"scope":"public_group","title":"...","content":"...","is_anniversary":false}],'
            '"persona_advices":[{"title":"...","advice":"...","reason":"..."}]}\n\n'
            + transcript
        )
        try:
            res = await self.model_hub.chat_complete(
                session,
                slot="reflection",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=600,
                source="reflection",
            )
            return json.loads(res.content)
        except Exception as exc:
            logger.warning("Nightly reflection LLM extraction failed [%s]: %s", type(exc).__name__, exc)
            return {}
