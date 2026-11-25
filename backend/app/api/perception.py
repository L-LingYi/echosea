"""Phase 2 & 3 Operations API: Hot-Topic Perception, Nightly Reflection, Life Schedule, Plugins, Backups."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, get_runtime
from app.models.db import BackupRecord, DailyBriefing, HotTopicSource, LifeScheduleSlot

router = APIRouter(prefix="/api/ops", tags=["perception_schedule_plugins"])


# --- Internet Hot-Topic Slow & Fast Channels ---
@router.get("/hot-sources")
async def list_hot_sources(_admin=Depends(get_current_admin), session: AsyncSession = Depends(get_db)):
    rows = list((await session.execute(select(HotTopicSource).order_by(HotTopicSource.id.asc()))).scalars().all())
    return [{"id": r.id, "name": r.name, "category": r.category, "feed_url": r.feed_url, "is_enabled": r.is_enabled} for r in rows]


@router.post("/hot-sources")
async def add_hot_source(body: dict[str, Any], _admin=Depends(get_current_admin), session: AsyncSession = Depends(get_db)):
    src = HotTopicSource(
        name=str(body.get("name", "热榜源")),
        category=str(body.get("category", "general")),
        feed_url=str(body.get("feed_url", "")),
        is_enabled=bool(body.get("is_enabled", True)),
    )
    session.add(src)
    await session.flush()
    return {"ok": True, "id": src.id}


@router.post("/briefings/refresh")
async def refresh_briefing(
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    briefing = await runtime.web_perception.refresh_daily_briefing(session)
    if briefing is None:
        return {"ok": False, "message": "No enabled hot-topic sources or items fetched"}
    return {"ok": True, "id": briefing.id, "digest_text": briefing.digest_text}


@router.get("/briefings/latest")
async def get_latest_briefing(_admin=Depends(get_current_admin), session: AsyncSession = Depends(get_db)):
    row = (await session.execute(select(DailyBriefing).order_by(DailyBriefing.created_at.desc()).limit(1))).scalar_one_or_none()
    if row is None:
        return {"digest_text": "", "source_items": []}
    return {"id": row.id, "briefing_date": row.briefing_date.isoformat(), "digest_text": row.digest_text, "source_items": row.source_items}


# --- Nightly Offline Reflection Engine ---
@router.post("/reflection/run")
async def run_reflection(
    body: dict[str, Any] | None = None,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    body = body or {}
    return await runtime.reflection_service.run_nightly_reflection(
        session=session,
        group_id=body.get("group_id"),
        lookback_hours=int(body.get("lookback_hours", 24)),
    )


# --- Character Life Schedule State Machine ---
@router.get("/schedule")
async def list_schedule(
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    slots = await runtime.life_schedule.ensure_default_slots(session)
    return [
        {
            "id": s.id,
            "start_hour": s.start_hour,
            "end_hour": s.end_hour,
            "activity": s.activity,
            "default_mood": s.default_mood,
            "energy_target": s.energy_target,
            "is_sleeping": s.is_sleeping,
        }
        for s in slots
    ]


@router.post("/schedule/sync")
async def sync_schedule_state(
    body: dict[str, Any] | None = None,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    body = body or {}
    state = await runtime.life_schedule.sync_current_state(
        session=session,
        local_hour=body.get("local_hour"),
        diary_note=str(body.get("diary_note") or ""),
    )
    return {
        "ok": True,
        "energy_level": state.energy_level,
        "mood": state.mood,
        "current_activity": state.current_activity,
        "diary_entry": state.diary_entry,
    }


# --- Plugins & Remote Backup Audit ---
@router.get("/plugins")
async def list_plugins(_admin=Depends(get_current_admin), runtime=Depends(get_runtime)):
    return {
        "loaded_plugins": runtime.plugin_manager.list_loaded(),
        "bus_hooks": runtime.event_bus.list_plugins(),
    }


@router.get("/backups")
async def list_backups(_admin=Depends(get_current_admin), session: AsyncSession = Depends(get_db)):
    rows = list((await session.execute(select(BackupRecord).order_by(BackupRecord.created_at.desc()).limit(30))).scalars().all())
    return [
        {
            "id": r.id,
            "filename": r.filename,
            "remote_target": r.remote_target,
            "size_bytes": r.size_bytes,
            "status": r.status,
            "error_message": r.error_message,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.post("/backups/trigger")
async def trigger_backup_hook(
    body: dict[str, Any] | None = None,
    _admin=Depends(get_current_admin),
    runtime=Depends(get_runtime),
):
    payload = body or {"filename": "manual_snapshot.sql.gz", "remote_target": "aliyun_webdav"}
    res = await runtime.event_bus.emit("on_backup_requested", payload)
    return {"ok": True, "bus_result": res}
