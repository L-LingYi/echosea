"""Nightly Offline Reflection Engine:
Distills L1/L2 ambient group streams into:
1. Acquaintance profile enrichments (interests, schedule cues).
2. Candidate group slang (pending WebUI review).
3. Shared episodic memories & interaction anniversaries.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import local_now
from app.core.model_hub import ModelHubService
from app.models.db import ChatMessage, GroupExpression, MemoryEpisode, QQIdentity

SLANG_PATTERN = re.compile(r"「([^」]{2,10})」|“([^”]{2,10})”")
INTEREST_CUES = {
    "原神": "原神",
    "崩铁": "崩坏：星穹铁道",
    "写代码": "编程开发",
    "露营": "户外露营",
    "火锅": "火锅美食",
    "烧烤": "烧烤",
    "健身": "健身",
    "猫": "吸猫",
}
ANNIVERSARY_CUES = ("周年", "纪念日", "生日", "第一次", "认识一")


class ReflectionService:
    """Runs offline reflection using the 'reflection' model slot (or deterministic fallback extractor)."""

    def __init__(self, model_hub: ModelHubService, timezone_name: str = "Asia/Shanghai") -> None:
        self.model_hub = model_hub
        self.timezone_name = timezone_name

    async def run_nightly_reflection(
        self,
        session: AsyncSession,
        group_id: int | None = None,
        lookback_hours: int = 24,
    ) -> dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
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
            return {"processed_messages": 0, "profiles_updated": 0, "slang_discovered": 0, "episodes_created": 0}

        profiles_updated = 0
        slang_discovered = 0
        episodes_created = 0

        # 1. Try reflection LLM slot if configured; otherwise run deterministic NLP extraction
        llm_extracted = await self._try_llm_reflection(session, msgs)

        # 2. Enrich acquaintance public profiles from group conversations (never touch private_profile from group)
        user_interests: dict[int, set[str]] = {}
        for m in msgs:
            if m.scene != "group":
                continue
            for kw, label in INTEREST_CUES.items():
                if kw in m.raw_text:
                    user_interests.setdefault(m.sender_qq, set()).add(label)

        for item in llm_extracted.get("profile_clues", []):
            qq = int(item.get("qq_id", 0))
            tag = str(item.get("interest", "")).strip()
            if qq and tag:
                user_interests.setdefault(qq, set()).add(tag)

        for qq, interests in user_interests.items():
            ident = await session.get(QQIdentity, qq)
            if ident is None:
                continue
            pub = dict(ident.public_profile or {})
            existing = list(pub.get("interests", []))
            merged = list(dict.fromkeys([*existing, *sorted(interests)]))
            if merged != existing:
                pub["interests"] = merged
                ident.public_profile = pub
                profiles_updated += 1

        # 3. Discover candidate group slang (status='pending' for WebUI moderation)
        slang_counter: Counter[tuple[int, str]] = Counter()
        for m in msgs:
            if m.scene == "group" and m.group_id:
                for match in SLANG_PATTERN.finditer(m.raw_text):
                    phrase = (match.group(1) or match.group(2) or "").strip()
                    if phrase:
                        slang_counter[(m.group_id, phrase)] += 1
        for item in llm_extracted.get("slang_candidates", []):
            gid = int(item.get("group_id") or group_id or 0)
            phrase = str(item.get("phrase", "")).strip()
            if gid and phrase:
                slang_counter[(gid, phrase)] += 2

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

        # 4. Extract episodic milestones & interaction anniversaries
        today_local = local_now(self.timezone_name).date()
        for m in msgs:
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
            session.add(
                MemoryEpisode(
                    scope="public_group",
                    group_id=ep.get("group_id") or group_id,
                    qq_id=ep.get("qq_id"),
                    title=str(ep.get("title", "共同经历"))[:200],
                    content=content,
                    importance=float(ep.get("importance", 0.7)),
                    is_anniversary=bool(ep.get("is_anniversary", False)),
                    event_date=today_local,
                )
            )
            episodes_created += 1

        await session.flush()
        return {
            "processed_messages": len(msgs),
            "profiles_updated": profiles_updated,
            "slang_discovered": slang_discovered,
            "episodes_created": episodes_created,
        }

    async def _try_llm_reflection(self, session: AsyncSession, msgs: list[ChatMessage]) -> dict[str, Any]:
        transcript = "\n".join(f"[{m.group_id or 'DM'}][{m.sender_qq}]{m.sender_display_name}: {m.raw_text}" for m in msgs[-60:])
        prompt = (
            "请从以下聊天记录提炼 JSON：{\"profile_clues\":[{\"qq_id\":123,\"interest\":\"...\"}],"
            "\"slang_candidates\":[{\"group_id\":123,\"phrase\":\"...\"}],"
            "\"episodes\":[{\"title\":\"...\",\"content\":\"...\",\"is_anniversary\":false}]}\n\n"
            + transcript
        )
        try:
            res = await self.model_hub.chat_complete(
                session,
                slot="reflection",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=500,
            )
            return json.loads(res.content)
        except Exception:
            return {}
