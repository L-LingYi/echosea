"""Unified Core Scheduler for periodic LifeSchedule, Daily Briefing, Nightly Reflection, and Backups."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import resolve_timezone
from app.models.db import SystemConfig

logger = logging.getLogger(__name__)

SCHEDULER_SCOPE_TYPE = "global"
SCHEDULER_SCOPE_ID = "core_scheduler"


class CoreScheduler:
    """Runs periodic background maintenance with persistent idempotency markers in SystemConfig."""

    def __init__(
        self,
        life_schedule: Any,
        web_perception: Any,
        reflection_service: Any,
        backup_service: Any,
        alert_service: Any | None = None,
        timezone_name: str = "Asia/Shanghai",
        schedule_interval_seconds: int = 900,
    ) -> None:
        self.life_schedule = life_schedule
        self.web_perception = web_perception
        self.reflection_service = reflection_service
        self.backup_service = backup_service
        self.alert_service = alert_service
        self.timezone_name = timezone_name
        self.schedule_interval_seconds = schedule_interval_seconds

    async def _load_state(self, session: AsyncSession) -> tuple[SystemConfig, dict[str, Any]]:
        stmt = select(SystemConfig).where(
            SystemConfig.scope_type == SCHEDULER_SCOPE_TYPE,
            SystemConfig.scope_id == SCHEDULER_SCOPE_ID,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = SystemConfig(
                scope_type=SCHEDULER_SCOPE_TYPE,
                scope_id=SCHEDULER_SCOPE_ID,
                config_json={},
            )
            session.add(row)
            await session.flush()
        return row, dict(row.config_json or {})

    async def run_tick(self, session: AsyncSession, now: datetime | None = None) -> dict[str, Any]:
        """Evaluate all due maintenance jobs and update persistent markers."""
        tz = resolve_timezone(self.timezone_name)
        utc_now = (now.astimezone(timezone.utc) if now is not None and now.tzinfo else (now.replace(tzinfo=timezone.utc) if now else datetime.now(timezone.utc)))
        local_dt = utc_now.astimezone(tz)
        today_str = local_dt.date().isoformat()
        hhmm = (local_dt.hour, local_dt.minute)

        cfg_row, state = await self._load_state(session)
        executed: list[str] = []

        # 1. Life Schedule sync (every 15 minutes)
        last_sync_raw = state.get("last_schedule_sync_at")
        sync_due = True
        if last_sync_raw:
            try:
                last_sync = datetime.fromisoformat(str(last_sync_raw))
                sync_due = (utc_now - last_sync) >= timedelta(seconds=self.schedule_interval_seconds)
            except ValueError:
                sync_due = True
        if sync_due and self.life_schedule is not None:
            try:
                await self.life_schedule.sync_current_state(session, local_hour=local_dt.hour)
                state["last_schedule_sync_at"] = utc_now.isoformat()
                executed.append("life_schedule")
            except Exception as exc:
                logger.warning("CoreScheduler life_schedule sync failed [%s]: %s", type(exc).__name__, exc)

        # 2. Morning Daily Briefing refresh (after 08:10 local time, once per day)
        if hhmm >= (8, 10) and state.get("last_briefing_date") != today_str and self.web_perception is not None:
            try:
                await self.web_perception.refresh_daily_briefing(session)
                state["last_briefing_date"] = today_str
                executed.append("daily_briefing")
            except Exception as exc:
                logger.warning("CoreScheduler daily_briefing failed [%s]: %s", type(exc).__name__, exc)

        # 3. Nightly Reflection & Anniversary Extraction (after 03:30 local time, once per day)
        if hhmm >= (3, 30) and state.get("last_reflection_date") != today_str and self.reflection_service is not None:
            try:
                await self.reflection_service.run_nightly_reflection(session)
                state["last_reflection_date"] = today_str
                executed.append("nightly_reflection")
            except Exception as exc:
                logger.warning("CoreScheduler nightly_reflection failed [%s]: %s", type(exc).__name__, exc)

        # 4. Scheduled Backup check
        if self.backup_service is not None:
            try:
                rec = await self.backup_service.run_scheduled_if_due(session, alert_service=self.alert_service)
                state["last_backup_check_at"] = utc_now.isoformat()
                if rec is not None:
                    executed.append("scheduled_backup")
            except Exception as exc:
                logger.warning("CoreScheduler scheduled_backup failed [%s]: %s", type(exc).__name__, exc)

        if state != (cfg_row.config_json or {}):
            cfg_row.config_json = state
            await session.flush()

        return {"local_time": local_dt.isoformat(), "executed": executed, "state": state}

    async def run_loop(
        self,
        session_factory: Any,
        stop_event: asyncio.Event,
        tick_seconds: float = 60.0,
    ) -> None:
        """Background scheduler loop waking every tick_seconds until stop_event is set."""
        while not stop_event.is_set():
            try:
                async with session_factory() as session:
                    await self.run_tick(session)
                    await session.commit()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("CoreScheduler tick error [%s]: %s", type(exc).__name__, exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
            except asyncio.TimeoutError:
                continue
