"""Multimodal Vision Service: Perceptual Hash (dHash) dedup cache & 3x3 GIF frame-extraction grid."""
from __future__ import annotations

import base64
import io
from datetime import datetime, timezone
from typing import Any

from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ImageVisionCache


def compute_dhash(image_bytes: bytes, hash_size: int = 8) -> str:
    """Compute a 64-bit difference hash (dHash) hex string for perceptual deduplication."""
    with Image.open(io.BytesIO(image_bytes)) as img:
        gray = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
        pixels = list(gray.getdata())
        bits = 0
        for row in range(hash_size):
            row_start = row * (hash_size + 1)
            for col in range(hash_size):
                bits <<= 1
                if pixels[row_start + col] > pixels[row_start + col + 1]:
                    bits |= 1
        return f"{bits:0{hash_size * hash_size // 4}x}"


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """Return Hamming distance between two hex dHashes."""
    if not hash_a or not hash_b or len(hash_a) != len(hash_b):
        return 64
    try:
        return (int(hash_a, 16) ^ int(hash_b, 16)).bit_count()
    except ValueError:
        return 64


def stitch_gif_3x3_grid(gif_bytes: bytes, tile_size: int = 160) -> tuple[bytes, int]:
    """Extract up to 9 evenly-spaced frames from an animated GIF and stitch into a 3x3 JPEG collage."""
    with Image.open(io.BytesIO(gif_bytes)) as img:
        n_frames = getattr(img, "n_frames", 1)
        if n_frames <= 1:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            return buf.getvalue(), 1

        sample_count = min(9, n_frames)
        if sample_count == 1:
            indices = [0]
        else:
            indices = [round(i * (n_frames - 1) / (sample_count - 1)) for i in range(sample_count)]

        grid = Image.new("RGB", (tile_size * 3, tile_size * 3), (245, 245, 245))
        for idx, frame_no in enumerate(indices):
            img.seek(frame_no)
            frame = img.convert("RGB").resize((tile_size, tile_size), Image.Resampling.LANCZOS)
            row, col = divmod(idx, 3)
            grid.paste(frame, (col * tile_size, row * tile_size))

        out = io.BytesIO()
        grid.save(out, format="JPEG", quality=85)
        return out.getvalue(), sample_count


class MultimodalService:
    """Prepares images & 3x3 GIF collages for native multimodal LLMs with dHash vision caching."""

    async def lookup_or_prepare(
        self,
        session: AsyncSession,
        image_bytes: bytes,
        source_url: str = "",
        is_gif: bool = False,
    ) -> dict[str, Any]:
        phash = compute_dhash(image_bytes)
        stmt = select(ImageVisionCache).where(ImageVisionCache.phash == phash)
        cached = (await session.execute(stmt)).scalar_one_or_none()
        if cached is not None and cached.description:
            cached.hit_count += 1
            cached.last_used_at = datetime.now(timezone.utc)
            await session.flush()
            return {
                "cache_hit": True,
                "phash": phash,
                "description": cached.description,
                "content_block": None,
            }

        frames_sampled = 1
        payload_bytes = image_bytes
        if is_gif:
            payload_bytes, frames_sampled = stitch_gif_3x3_grid(image_bytes)

        b64 = base64.b64encode(payload_bytes).decode("ascii")
        if cached is None:
            cached = ImageVisionCache(
                phash=phash,
                source_url=source_url,
                is_gif_grid=is_gif and frames_sampled > 1,
                description="",
            )
            session.add(cached)
            await session.flush()

        return {
            "cache_hit": False,
            "phash": phash,
            "is_gif_grid": is_gif and frames_sampled > 1,
            "frames_sampled": frames_sampled,
            "content_block": {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            },
        }

    async def save_vision_description(self, session: AsyncSession, phash: str, description: str) -> None:
        stmt = select(ImageVisionCache).where(ImageVisionCache.phash == phash)
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            row.description = description.strip()
            row.last_used_at = datetime.now(timezone.utc)
            await session.flush()
