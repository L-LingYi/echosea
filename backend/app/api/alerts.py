"""24h Alerting Channels & History API."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, get_runtime
from app.models.db import AlertChannel, AlertEvent
from app.models.schemas import AlertChannelUpsert
from app.security import encrypt_secret

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("/channels")
async def list_channels(
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    rows = list((await session.execute(select(AlertChannel).order_by(AlertChannel.id.asc()))).scalars().all())
    return [
        {
            "id": r.id,
            "channel_type": r.channel_type,
            "name": r.name,
            "is_enabled": r.is_enabled,
            "updated_at": r.updated_at.isoformat(),
        }
        for r in rows
    ]


@router.post("/channels")
async def create_channel(
    payload: AlertChannelUpsert,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    enc = encrypt_secret(json.dumps(payload.config, ensure_ascii=False), runtime.settings.master_encryption_key)
    ch = AlertChannel(
        channel_type=payload.channel_type,
        name=payload.name,
        config_enc=enc,
        is_enabled=payload.is_enabled,
    )
    session.add(ch)
    await session.flush()
    return {"ok": True, "id": ch.id}


@router.delete("/channels/{channel_id}")
async def delete_channel(
    channel_id: int,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    ch = await session.get(AlertChannel, channel_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="Alert channel not found")
    await session.delete(ch)
    return {"ok": True}


@router.post("/test")
async def send_test_alert(
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    event = await runtime.alert_service.notify(
        session=session,
        severity="info",
        category="manual_test",
        title="告警通道测试通知",
        detail="这是一条来自 Companion System v1.0 云端控制台的测试告警消息。",
        bypass_cooldown=True,
    )
    return {"ok": True, "delivered_channels": event.delivered_channels if event else []}


@router.get("/events")
async def list_alert_events(
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    rows = list((await session.execute(select(AlertEvent).order_by(AlertEvent.created_at.desc()).limit(50))).scalars().all())
    return [
        {
            "id": r.id,
            "severity": r.severity,
            "category": r.category,
            "title": r.title,
            "detail": r.detail,
            "delivered_channels": r.delivered_channels,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
