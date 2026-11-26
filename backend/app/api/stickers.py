"""Sticker Gallery Management & Batch Import API."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, get_runtime
from app.core.web_perception_service import _is_public_http_url
from app.models.db import StickerAsset

router = APIRouter(prefix="/api/stickers", tags=["stickers"])


def _detect_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return "application/octet-stream"


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


@router.get("/{sticker_id}/image")
async def get_sticker_image(
    sticker_id: int,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    """Serve cached local sticker bytes, or proxy & backfill from source_url without browser Referer issues."""
    asset = await session.get(StickerAsset, sticker_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Sticker not found")

    if asset.file_path:
        p = Path(asset.file_path)
        if p.is_file():
            data = p.read_bytes()
            if data:
                return Response(
                    content=data,
                    media_type=_detect_image_mime(data),
                    headers={"Cache-Control": "private, max-age=86400"},
                )

    url = (asset.source_url or "").strip()
    if not url or not _is_public_http_url(url):
        raise HTTPException(status_code=404, detail="Sticker image unavailable")

    try:
        async with httpx.AsyncClient(
            timeout=6.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
    except Exception:
        raise HTTPException(status_code=404, detail="Upstream sticker image unavailable")

    if not data or len(data) > 4 * 1024 * 1024:
        raise HTTPException(status_code=404, detail="Invalid sticker image payload")
    mime = _detect_image_mime(data)
    if mime == "application/octet-stream":
        ct = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not ct.startswith("image/"):
            raise HTTPException(status_code=404, detail="Upstream response is not an image")
        mime = ct

    # Backfill local cache so future views survive QQ rkey expiration
    try:
        storage_dir = getattr(getattr(runtime, "sticker_service", None), "storage_dir", Path("data/stickers"))
        Path(storage_dir).mkdir(parents=True, exist_ok=True)
        safe_name = "".join(ch for ch in (asset.phash or f"stk_{asset.id}") if ch.isalnum() or ch in ("_", "-"))[:64]
        dest = Path(storage_dir) / f"{safe_name}.bin"
        dest.write_bytes(data)
        asset.file_path = str(dest)
        await session.flush()
    except OSError:
        pass

    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=86400"},
    )


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
    runtime=Depends(get_runtime),
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
    if getattr(runtime, "sticker_service", None) is not None:
        runtime.sticker_service.invalidate_tag_cache()
    return {"ok": True, "id": asset.id, "status": asset.status}


@router.delete("/{sticker_id}")
async def delete_sticker(
    sticker_id: int,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    asset = await session.get(StickerAsset, sticker_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Sticker not found")
    file_path = (asset.file_path or "").strip()
    await session.delete(asset)
    svc = getattr(runtime, "sticker_service", None)
    if svc is not None:
        svc.invalidate_tag_cache()
        if file_path:
            try:
                base_dir = Path(svc.storage_dir).resolve()
                target = Path(file_path).resolve()
                if target.is_relative_to(base_dir) and target.is_file():
                    target.unlink(missing_ok=True)
            except OSError:
                pass
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
