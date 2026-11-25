"""Sticker Gallery Management & Batch Import API."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, get_runtime
from app.models.db import StickerAsset

router = APIRouter(prefix="/api/stickers", tags=["stickers"])


@router.get("")
async def list_stickers(
    status: str | None = None,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    stmt = select(StickerAsset).order_by(StickerAsset.positive_reactions.desc(), StickerAsset.id.desc()).limit(200)
    if status:
        stmt = stmt.where(StickerAsset.status == status)
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        {
            "id": r.id,
            "phash": r.phash,
            "source_url": r.source_url,
            "file_path": r.file_path,
            "emotion_tags": r.emotion_tags,
            "context_hint": r.context_hint,
            "appearance_count": r.appearance_count,
            "positive_reactions": r.positive_reactions,
            "origin": r.origin,
            "status": r.status,
        }
        for r in rows
    ]


@router.post("/import")
async def batch_import_stickers(
    body: dict[str, Any],
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    items = body.get("items") or []
    created = await runtime.sticker_service.import_manual(session, items)
    return {"ok": True, "imported_count": len(created)}


@router.patch("/{sticker_id}")
async def review_sticker(
    sticker_id: int,
    body: dict[str, Any],
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    asset = await session.get(StickerAsset, sticker_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Sticker not found")
    if "status" in body:
        asset.status = str(body["status"])
    if "emotion_tags" in body and isinstance(body["emotion_tags"], list):
        asset.emotion_tags = body["emotion_tags"]
    if "context_hint" in body:
        asset.context_hint = str(body["context_hint"])
    return {"ok": True, "id": asset.id, "status": asset.status}


@router.delete("/{sticker_id}")
async def delete_sticker(
    sticker_id: int,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    asset = await session.get(StickerAsset, sticker_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Sticker not found")
    await session.delete(asset)
    return {"ok": True}
