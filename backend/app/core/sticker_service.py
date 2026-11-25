"""Sticker Auto-Learning, Perceptual Hash Dedup, Gallery Review, and Reply Tag Trigger."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.multimodal_service import compute_dhash, hamming_distance
from app.models.db import StickerAsset

POSITIVE_REACTION_CUES = ("哈哈", "草", "笑死", "偷了", "好图", "乐", "典", "绷不住", "666", "收了")
STICKER_TAG_RE = re.compile(r"\[(?:sticker|表情)[:：]([^\]\s]+)\]", re.IGNORECASE)


def extract_sticker_tags(text: str) -> tuple[str, list[str]]:
    """Extract lightweight sticker trigger tags like [sticker:laugh] or [表情:无语] from reply text."""
    tags = [m.group(1).strip() for m in STICKER_TAG_RE.finditer(text)]
    cleaned = STICKER_TAG_RE.sub("", text).strip()
    return cleaned, tags


class StickerService:
    """Manages auto-stolen and manually imported stickers with dHash deduplication."""

    def __init__(self, storage_dir: str | Path = "data/stickers", auto_approve_threshold: int = 3) -> None:
        self.storage_dir = Path(storage_dir)
        self.auto_approve_threshold = auto_approve_threshold
        self._recent_group_sticker: dict[int, str] = {}  # group_id -> last seen phash

    async def observe_group_image(
        self,
        session: AsyncSession,
        group_id: int,
        image_bytes: bytes | None = None,
        phash: str | None = None,
        source_url: str = "",
        emotion_tags: list[str] | None = None,
        context_hint: str = "",
    ) -> StickerAsset:
        """Record an image seen in group chat; deduplicate via perceptual hash."""
        computed_hash = phash or (compute_dhash(image_bytes) if image_bytes else "")
        if not computed_hash:
            raise ValueError("Either image_bytes or phash is required")

        # Check exact or near-duplicate dHash (Hamming distance <= 4)
        existing_rows = list((await session.execute(select(StickerAsset))).scalars().all())
        match: StickerAsset | None = None
        for row in existing_rows:
            if row.phash == computed_hash or hamming_distance(row.phash, computed_hash) <= 4:
                match = row
                break

        if match is not None:
            match.appearance_count += 1
            if emotion_tags:
                merged = list(dict.fromkeys([*(match.emotion_tags or []), *emotion_tags]))
                match.emotion_tags = merged
            if match.status == "pending" and (
                match.appearance_count >= self.auto_approve_threshold
                or match.positive_reactions >= 2
            ):
                match.status = "approved"
            self._recent_group_sticker[group_id] = match.phash
            await session.flush()
            return match

        file_path = ""
        if image_bytes:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            dest = self.storage_dir / f"{computed_hash}.bin"
            dest.write_bytes(image_bytes)
            file_path = str(dest)

        asset = StickerAsset(
            phash=computed_hash,
            file_path=file_path,
            source_url=source_url,
            source_group_id=group_id,
            emotion_tags=emotion_tags or ["fun"],
            context_hint=context_hint,
            appearance_count=1,
            positive_reactions=0,
            origin="auto",
            status="pending",
        )
        session.add(asset)
        self._recent_group_sticker[group_id] = computed_hash
        await session.flush()
        return asset

    async def observe_group_reaction_text(
        self,
        session: AsyncSession,
        group_id: int,
        text: str,
    ) -> StickerAsset | None:
        """If group members react positively right after a sticker, boost its score and auto-steal."""
        if not any(cue in text for cue in POSITIVE_REACTION_CUES):
            return None
        last_hash = self._recent_group_sticker.get(group_id)
        if not last_hash:
            return None
        stmt = select(StickerAsset).where(StickerAsset.phash == last_hash)
        asset = (await session.execute(stmt)).scalar_one_or_none()
        if asset is None:
            return None
        asset.positive_reactions += 1
        if asset.status == "pending" and asset.positive_reactions >= 2:
            asset.status = "approved"
        await session.flush()
        return asset

    async def import_manual(
        self,
        session: AsyncSession,
        items: list[dict[str, Any]],
    ) -> list[StickerAsset]:
        """Batch manual import from WebUI (immediately approved)."""
        created: list[StickerAsset] = []
        for item in items:
            phash = str(item.get("phash") or "").strip()
            source_url = str(item.get("source_url") or "").strip()
            if not phash:
                phash = f"manual_{abs(hash(source_url)) % (10**16):016x}"
            existing = (await session.execute(select(StickerAsset).where(StickerAsset.phash == phash))).scalar_one_or_none()
            if existing is not None:
                existing.emotion_tags = list(dict.fromkeys([*(existing.emotion_tags or []), *(item.get("emotion_tags") or [])]))
                existing.status = "approved"
                created.append(existing)
                continue
            asset = StickerAsset(
                phash=phash,
                source_url=source_url,
                file_path=str(item.get("file_path") or ""),
                emotion_tags=item.get("emotion_tags") or ["general"],
                context_hint=str(item.get("context_hint") or ""),
                origin="manual",
                status="approved",
            )
            session.add(asset)
            created.append(asset)
        await session.flush()
        return created

    async def pick_sticker_for_tag(self, session: AsyncSession, tag: str) -> StickerAsset | None:
        """Select an approved sticker matching the requested emotion/context tag."""
        stmt = (
            select(StickerAsset)
            .where(StickerAsset.status == "approved")
            .order_by(StickerAsset.positive_reactions.desc(), StickerAsset.appearance_count.desc())
        )
        rows = list((await session.execute(stmt)).scalars().all())
        tag_lower = tag.lower()
        for row in rows:
            tags = [str(t).lower() for t in (row.emotion_tags or [])]
            if tag_lower in tags or tag_lower in (row.context_hint or "").lower():
                return row
        return rows[0] if rows else None
