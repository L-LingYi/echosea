"""Phase 2 & 3 Operations API: Hot-Topic Perception, Nightly Reflection, Life Schedule, Plugins, Backups."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
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


HOT_SOURCE_PRESETS = [
    {"name": "36氪快讯", "category": "tech", "feed_url": "https://36kr.com/feed"},
    {"name": "少数派", "category": "lifestyle", "feed_url": "https://sspai.com/feed"},
    {"name": "V2EX 最热", "category": "community", "feed_url": "https://www.v2ex.com/index.xml"},
    {"name": "机核网", "category": "acg", "feed_url": "https://www.gcores.com/rss"},
]


@router.get("/hot-sources/presets")
async def list_hot_source_presets(_admin=Depends(get_current_admin)):
    return HOT_SOURCE_PRESETS


@router.patch("/hot-sources/{source_id}")
async def update_hot_source(
    source_id: int,
    body: dict[str, Any],
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    src = await session.get(HotTopicSource, source_id)
    if src is None:
        raise HTTPException(status_code=404, detail="Hot source not found")
    if "is_enabled" in body:
        src.is_enabled = bool(body["is_enabled"])
    if "name" in body and body["name"]:
        src.name = str(body["name"])
    if "category" in body and body["category"]:
        src.category = str(body["category"])
    if "feed_url" in body and body["feed_url"]:
        src.feed_url = str(body["feed_url"])
    await session.flush()
    return {"ok": True, "id": src.id, "is_enabled": src.is_enabled}


@router.delete("/hot-sources/{source_id}")
async def delete_hot_source(
    source_id: int,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    src = await session.get(HotTopicSource, source_id)
    if src is None:
        raise HTTPException(status_code=404, detail="Hot source not found")
    await session.delete(src)
    return {"ok": True}


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


@router.get("/backup-config")
async def get_backup_config(
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    return await runtime.backup_service.get_safe_config(session)


@router.put("/backup-config")
async def save_backup_config(
    body: dict[str, Any],
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    return await runtime.backup_service.save_config(session, body)


@router.post("/backup-config/oauth-url")
async def get_aliyun_oauth_url(
    body: dict[str, Any] | None = None,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    redirect_uri = str((body or {}).get("redirect_uri") or "oob")
    return await runtime.backup_service.build_aliyun_oauth_url(session, redirect_uri=redirect_uri)


@router.post("/backup-config/exchange-code")
async def exchange_aliyun_oauth_code(
    body: dict[str, Any],
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    code = str(body.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="请提供授权回调 code")
    return await runtime.backup_service.exchange_aliyun_code(session, code)


@router.post("/backup-config/test")
async def test_backup_connection(
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    return await runtime.backup_service.test_connection(session)


@router.post("/backups/trigger")
async def trigger_backup_hook(
    body: dict[str, Any] | None = None,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    body = body or {}
    custom_name = body.get("filename")
    try:
        rec = await runtime.backup_service.run_backup(session, custom_filename=custom_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if rec.status == "failed":
        await runtime.alert_service.notify(
            session,
            severity="critical",
            category="backup_failure",
            title=f"异地备份上传失败：{rec.filename}",
            detail=f"目标：{rec.remote_target}，原因：{rec.error_message or '未知错误'}",
        )
    bus_payload = {"filename": rec.filename, "remote_target": rec.remote_target, "status": rec.status}
    if body.get("simulated_error"):
        bus_payload["simulated_error"] = body["simulated_error"]
    bus_res = await runtime.event_bus.emit("on_backup_requested", bus_payload)
    return {
        "ok": rec.status == "success",
        "id": rec.id,
        "filename": rec.filename,
        "remote_target": rec.remote_target,
        "size_bytes": rec.size_bytes,
        "status": rec.status,
        "error_message": rec.error_message,
        "bus_result": bus_res,
    }


@router.post("/backups/restore")
async def restore_backup_snapshot(
    file: UploadFile = File(...),
    dry_run: bool = Query(default=True),
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    raw_bytes = await file.read()
    if len(raw_bytes) > 64 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="快照文件超过 64MB 限制")
    try:
        return await runtime.backup_service.verify_and_restore(session, raw_bytes, dry_run=dry_run)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
