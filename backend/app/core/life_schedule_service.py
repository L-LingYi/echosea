"""Character Circadian Life Schedule & State Machine (L5 Self Life Memory)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import local_now
from app.models.db import LifeScheduleSlot, SelfLifeState

DEFAULT_SCHEDULE = [
    {"start_hour": 0, "end_hour": 7, "activity": "睡觉补觉中", "default_mood": "sleepy", "energy_target": 25, "is_sleeping": True},
    {"start_hour": 7, "end_hour": 9, "activity": "刚醒迷糊吃早饭", "default_mood": "relaxed", "energy_target": 65, "is_sleeping": False},
    {"start_hour": 9, "end_hour": 12, "activity": "上午摸鱼刷群", "default_mood": "happy", "energy_target": 85, "is_sleeping": False},
    {"start_hour": 12, "end_hour": 14, "activity": "干饭加午休", "default_mood": "relaxed", "energy_target": 75, "is_sleeping": False},
    {"start_hour": 14, "end_hour": 18, "activity": "下午发呆顺便看群", "default_mood": "relaxed", "energy_target": 70, "is_sleeping": False},
    {"start_hour": 18, "end_hour": 22, "activity": "晚间打游戏看番吹水", "default_mood": "excited", "energy_target": 90, "is_sleeping": False},
    {"start_hour": 22, "end_hour": 24, "activity": "睡前刷手机困困的", "default_mood": "sleepy", "energy_target": 45, "is_sleeping": False},
]


class LifeScheduleService:
    """Drives the character's circadian rhythm, energy, mood, and daily activity."""

    def __init__(self, timezone_name: str = "Asia/Shanghai") -> None:
        self.timezone_name = timezone_name

    async def ensure_default_slots(self, session: AsyncSession) -> list[LifeScheduleSlot]:
        rows = list((await session.execute(select(LifeScheduleSlot).order_by(LifeScheduleSlot.start_hour.asc()))).scalars().all())
        if rows:
            return rows
        for item in DEFAULT_SCHEDULE:
            slot = LifeScheduleSlot(**item)
            session.add(slot)
            rows.append(slot)
        await session.flush()
        return rows

    async def sync_current_state(
        self,
        session: AsyncSession,
        local_hour: int | None = None,
        diary_note: str = "",
    ) -> SelfLifeState:
        """Advance the state machine for the given hour (0-23) and record a fresh L5 SelfLifeState."""
        slots = await self.ensure_default_slots(session)
        hour = local_hour if local_hour is not None else local_now(self.timezone_name).hour

        matched = None
        for s in slots:
            if s.start_hour <= hour < s.end_hour:
                matched = s
                break
        if matched is None:
            matched = slots[0]

        state = SelfLifeState(
            energy_level=matched.energy_target,
            mood=matched.default_mood,
            current_activity=matched.activity,
            diary_entry=diary_note or f"{hour:02d}:00 正在{matched.activity}",
        )
        session.add(state)
        await session.flush()
        return state
