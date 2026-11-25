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


@router.post("/{sticker_id}/auto-tag")
async def auto_tag_sticker(
    sticker_id: int,
    body: dict[str, Any] | None = None,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    """Trigger AI context understanding & automatic emotion tagging for a collected sticker."""
    from app.models.db import ChatMessage
    asset = await session.get(StickerAsset, sticker_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Sticker not found")
    body = body or {}
    surrounding = body.get("context_messages") or []
    if not surrounding and asset.source_group_id:
        rows = list(
            (
                await session.execute(
                    select(ChatMessage.sender_display_name, ChatMessage.raw_text)
                    .where(ChatMessage.group_id == asset.source_group_id, ChatMessage.is_recalled.is_(False))
                    .order_by(ChatMessage.id.desc())
                    .limit(6)
                )
            ).all()
        )
        surrounding = [f"{r[0]}: {r[1]}" for r in reversed(rows) if r[1]]
    if asset.context_hint:
        surrounding.append(asset.context_hint)
    updated = await runtime.sticker_service.auto_tag_with_context(
        session=session,
        asset=asset,
        surrounding_messages=surrounding,
        model_hub=runtime.model_hub,
    )
    return {
        "ok": True,
        "id": updated.id,
        "emotion_tags": updated.emotion_tags,
        "context_hint": updated.context_hint,
    }


@router.get("/presets")
async def list_sticker_presets(_admin=Depends(get_current_admin)):
    from app.core.sticker_service import PUBLIC_STICKER_PRESETS
    return PUBLIC_STICKER_PRESETS


@router.post("/presets/import")
async def import_sticker_preset(
    body: dict[str, Any],
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    try:
        return await runtime.sticker_service.import_from_preset(
            session=session,
            preset_id=body.get("preset_id"),
            custom_url=body.get("custom_url"),
            limit=int(body.get("limit", 80)),
            fetcher=getattr(runtime, "preset_fetcher", None),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"拉取预设表情包失败：{exc}")


@router.post("/presets/upload")
async def upload_sticker_dataset(
    body: dict[str, Any],
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    """Import stickers from a user-downloaded local JSON / JSONL file."""
    content = str(body.get("content") or "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="上传文件内容为空")
    try:
        return await runtime.sticker_service.import_from_raw_text(
            session=session,
            raw_text=content,
            limit=int(body.get("limit", 80)),
            source_label=str(body.get("filename") or "local_upload"),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"解析上传的表情包文件失败：{exc}")
