"""Multimodal Vision Service: Perceptual Hash (dHash) dedup cache & 3x3 GIF frame-extraction grid."""
from __future__ import annotations

import base64
import hashlib
import io
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.web_perception_service import _is_public_http_url
from app.models.db import ImageVisionCache

logger = logging.getLogger(__name__)
MAX_VISION_IMAGE_BYTES = 5 * 1024 * 1024


def deterministic_url_hash(url: str, prefix: str = "url") -> str:
    """Return a deterministic 16-hex SHA256 digest tag for URL fallback deduplication."""
    digest = hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


async def download_image_bytes(
    url: str,
    timeout: float = 3.0,
    max_bytes: int = MAX_VISION_IMAGE_BYTES,
    fetcher: Callable[[str], Awaitable[bytes | None]] | None = None,
) -> bytes | None:
    """Fetch image bytes safely with SSRF validation, strict timeout, and size cap."""
    clean_url = (url or "").strip()
    if not clean_url:
        return None
    if fetcher is not None:
        try:
            data = await fetcher(clean_url)
            if data and len(data) <= max_bytes:
                return data
        except Exception as exc:
            logger.debug("Custom image fetcher failed for %s: %s", clean_url, exc)
        return None
    if not _is_public_http_url(clean_url):
        logger.debug("Blocked non-public image URL: %s", clean_url)
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream("GET", clean_url, headers={"User-Agent": "EchoSea-Vision/1.6"}) as resp:
                if resp.status_code >= 400:
                    return None
                cl = resp.headers.get("content-length")
                if cl and cl.isdigit() and int(cl) > max_bytes:
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        return None
                    chunks.append(chunk)
                data = b"".join(chunks)
                return data or None
    except Exception as exc:
        logger.debug("Image download failed for %s [%s]: %s", clean_url, type(exc).__name__, exc)
        return None


async def resolve_image_phash(
    url: str,
    timeout: float = 3.0,
    fetcher: Callable[[str], Awaitable[bytes | None]] | None = None,
) -> tuple[str, bytes | None]:
    """Download image bytes safely and compute real dHash; fallback to deterministic SHA256 on failure."""
    data = await download_image_bytes(url, timeout=timeout, fetcher=fetcher)
    if data:
        try:
            return compute_dhash(data), data
        except Exception as exc:
            logger.debug("compute_dhash failed for %s: %s", url, exc)
    return deterministic_url_hash(url, prefix="url"), None


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

    def __init__(self, image_fetcher: Callable[[str], Awaitable[bytes | None]] | None = None) -> None:
        self.image_fetcher = image_fetcher

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

    @staticmethod
    async def resolve_media_hints_map(
        session: AsyncSession,
        media_lists: list[list[dict[str, Any]] | None],
    ) -> dict[str, str]:
        """Batch-lookup cached vision descriptions by source_url for L1/Gatekeeper history."""
        urls: list[str] = []
        for items in media_lists:
            for m in items or []:
                if isinstance(m, dict) and m.get("type") in ("image", "mface") and m.get("url"):
                    urls.append(str(m["url"]))
        if not urls:
            return {}
        unique_urls = list(dict.fromkeys(urls))
        rows = (
            await session.execute(
                select(ImageVisionCache.source_url, ImageVisionCache.description).where(
                    ImageVisionCache.source_url.in_(unique_urls)
                )
            )
        ).all()
        return {str(u): str(d).strip() for u, d in rows if u and d and str(d).strip()}

    @staticmethod
    def format_media_placeholder(
        media_meta: list[dict[str, Any]] | None,
        url_desc_map: dict[str, str] | None = None,
    ) -> str:
        """Render media_meta into concise text tags like '[图片: 某手游截图]' or '[图片]'."""
        if not media_meta:
            return ""
        parts: list[str] = []
        desc_map = url_desc_map or {}
        for m in media_meta[:3]:
            if not isinstance(m, dict):
                continue
            mtype = m.get("type")
            if mtype in ("image", "mface"):
                url = str(m.get("url") or "")
                desc = desc_map.get(url, "").strip()
                parts.append(f"[图片: {desc[:48]}]" if desc else ("[表情包]" if mtype == "mface" else "[图片]"))
            elif mtype == "face":
                parts.append("[QQ表情]")
        return " ".join(parts)
