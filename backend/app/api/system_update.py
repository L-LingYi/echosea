"""System Online Update API: Status, GitHub/Local Check, 3-Tier Inspect, TOTP Confirm, Trigger & Callback."""
from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, get_runtime
from app.core.update_service import verify_updater_signature
from app.models.db import AdminUser, UpdateJob
from app.security import decrypt_secret, verify_password, verify_totp_code

router = APIRouter(prefix="/api/system/update", tags=["system_update"])


@router.get("/status")
async def update_status(
    admin: AdminUser = Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    svc = runtime.update_service
    await svc.reconcile_active_job(session)
    rows = list((await session.execute(select(UpdateJob).order_by(UpdateJob.created_at.desc()).limit(20))).scalars().all())
    auto_cfg = await svc.get_auto_update_config(session)
    return {
        "mode": svc.mode,
        "current_version": svc.current_version,
        "current_commit": svc.current_commit or "unknown",
        "github_repo": svc.github_repo,
        "git_branch": svc.git_branch,
        "updater_configured": bool(svc.updater_url and svc.updater_secret),
        "totp_enabled": bool(admin.totp_enabled),
        "auto_update": auto_cfg,
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
                "confirmation": svc.get_confirmation_detail(r.job_uid) if r.status == "confirmation_required" else None,
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


@router.post("/inspect")
async def inspect_updates(
    body: dict[str, Any] | None = None,
    _admin=Depends(get_current_admin),
    runtime=Depends(get_runtime),
):
    body = body or {}
    try:
        return await runtime.update_service.inspect_update(target_ref=body.get("target_ref"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"宿主机更新检查失败：{exc}")


@router.post("/confirm-token")
async def issue_confirm_token(
    body: dict[str, Any],
    admin: AdminUser = Depends(get_current_admin),
    runtime=Depends(get_runtime),
):
    """Verify TOTP (if enabled) or admin password, then issue a single-use host confirmation token."""
    if admin.totp_enabled and admin.totp_secret_enc:
        code = str(body.get("totp_code") or "").strip()
        secret = decrypt_secret(admin.totp_secret_enc, runtime.settings.master_encryption_key)
        if not verify_totp_code(secret, code):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="TOTP 动态验证码错误")
    else:
        pwd = str(body.get("password") or "")
        if not pwd or not verify_password(pwd, admin.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="管理员密码错误")

    try:
        target_ref = str(body.get("target_ref") or runtime.update_service.git_branch)
        to_commit = str(body.get("to_commit") or "")
        diff_sha256 = str(body.get("diff_sha256") or "")
        return {
            "ok": True,
            **runtime.update_service.issue_confirmation_token(
                admin_username=admin.username,
                target_ref=target_ref,
                to_commit=to_commit,
                diff_sha256=diff_sha256,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
            confirm_token=body.get("confirm_token"),
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


@router.get("/auto-config")
async def get_auto_update_config(
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    return await runtime.update_service.get_auto_update_config(session)


@router.put("/auto-config")
async def save_auto_update_config(
    body: dict[str, Any],
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    try:
        cfg = await runtime.update_service.save_auto_update_config(session, body)
        return {"ok": True, **cfg}
    except (ValueError, TypeError) as exc:
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
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 65536:
            raise HTTPException(status_code=413, detail="Updater callback is too large")
        body.extend(chunk)
    raw_body = bytes(body)
    secret = runtime.settings.update_webhook_secret
    if not verify_updater_signature(secret, x_updater_timestamp, x_updater_signature, raw_body):
        raise HTTPException(status_code=403, detail="Invalid or expired HMAC updater signature")
    try:
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise ValueError("Invalid update payload")
        job = await runtime.update_service.handle_callback(session, payload)
        return {"ok": True, "job_uid": job.job_uid, "status": job.status, "rolled_back": job.rolled_back}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
