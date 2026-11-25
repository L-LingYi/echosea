"""System Online Update API: Status, GitHub/Local Update Check, Signed Trigger, and Host Callback."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, get_runtime
from app.core.update_service import verify_updater_signature
from app.models.db import AdminUser, UpdateJob

router = APIRouter(prefix="/api/system/update", tags=["system_update"])


@router.get("/status")
async def update_status(
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    rows = list((await session.execute(select(UpdateJob).order_by(UpdateJob.created_at.desc()).limit(20))).scalars().all())
    svc = runtime.update_service
    return {
        "mode": svc.mode,
        "current_version": svc.current_version,
        "current_commit": svc.current_commit or "unknown",
        "github_repo": svc.github_repo,
        "git_branch": svc.git_branch,
        "updater_configured": bool(svc.updater_url and svc.updater_secret),
        "jobs": [
            {
                "job_uid": r.job_uid,
                "triggered_by": r.triggered_by,
                "mode": r.mode,
                "target_ref": r.target_ref,
                "from_version": r.from_version,
                "from_commit": r.from_commit,
                "to_commit": r.to_commit,
                "status": r.status,
                "rolled_back": r.rolled_back,
                "log_summary": r.log_summary,
                "created_at": r.created_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            }
            for r in rows
        ],
    }


@router.get("/check")
async def check_updates(
    _admin=Depends(get_current_admin),
    runtime=Depends(get_runtime),
):
    return await runtime.update_service.check_for_updates()


@router.post("/trigger")
async def trigger_update(
    body: dict[str, Any] | None = None,
    admin: AdminUser = Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    body = body or {}
    try:
        job = await runtime.update_service.trigger_update(
            session=session,
            triggered_by=admin.username,
            target_ref=body.get("target_ref"),
        )
        return {
            "ok": True,
            "job_uid": job.job_uid,
            "status": job.status,
            "target_ref": job.target_ref,
            "log_summary": job.log_summary,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/callback")
async def updater_callback(
    request: Request,
    x_updater_timestamp: str | None = Header(default=None),
    x_updater_signature: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    """HMAC-SHA256 authenticated callback from host updater daemon reporting success or auto-rollback."""
    raw_body = await request.body()
    secret = runtime.settings.update_webhook_secret
    if not verify_updater_signature(secret, x_updater_timestamp, x_updater_signature, raw_body):
        raise HTTPException(status_code=403, detail="Invalid or expired HMAC updater signature")
    payload = json.loads(raw_body.decode("utf-8"))
    try:
        job = await runtime.update_service.handle_callback(session, payload)
        return {"ok": True, "job_uid": job.job_uid, "status": job.status, "rolled_back": job.rolled_back}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
