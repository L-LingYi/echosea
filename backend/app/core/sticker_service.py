"""Sticker Auto-Learning, Perceptual Hash Dedup, Gallery Review, and Reply Tag Trigger."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.multimodal_service import compute_dhash, deterministic_url_hash, hamming_distance
from app.models.db import StickerAsset

logger = logging.getLogger(__name__)

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
                phash = deterministic_url_hash(source_url, prefix="manual")
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


    async def auto_tag_with_context(
        self,
        session: AsyncSession,
        asset: StickerAsset,
        surrounding_messages: list[str],
        model_hub: Any = None,
    ) -> StickerAsset:
        """Understand & auto-tag a collected sticker using surrounding group chat context (when & why members use it)."""
        context_blob = "\n".join(m for m in surrounding_messages[-6:] if m.strip())
        # 1. Try LLM understanding via reflection slot if available
        if model_hub is not None and context_blob:
            import json as _json
            prompt = (
                "根据下面群友发表情包前后的聊天上下文，总结这张表情包的适用情绪标签(2-4个简短中文词)和你对它使用场景的理解(30字以内)。\n"
                '仅输出 JSON：{"emotion_tags":["无语","吐槽"],"context_hint":"群友遇到离谱或踩坑时用来表达崩溃无语"}\n\n'
                f"上下文：\n{context_blob}"
            )
            try:
                res = await model_hub.chat_complete(
                    session,
                    slot="reflection",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=160,
                    source="reflection",
                )
                parsed = _json.loads(res.content)
                tags = [str(t).strip() for t in (parsed.get("emotion_tags") or []) if str(t).strip()][:5]
                hint = str(parsed.get("context_hint") or "").strip()[:160]
                if tags:
                    asset.emotion_tags = list(dict.fromkeys([*(asset.emotion_tags or []), *tags]))
                if hint:
                    asset.context_hint = hint
                await session.flush()
                return asset
            except Exception as exc:
                logger.debug("LLM sticker auto-tagging skipped/failed [%s]: %s", type(exc).__name__, exc)

        # 2. Deterministic context-aware heuristic tagger fallback
        cue_map = [
            (("哈哈", "笑死", "乐", "草", "绷不住"), "大笑", "群友被逗乐或看热闹起哄时使用"),
            (("欠费", "崩了", "寄了", "完了", "死机", "离谱"), "崩溃无语", "群友踩坑、报错或遇到离谱事时吐槽使用"),
            (("摸鱼", "困", "睡", "累", "下班"), "摸鱼摆烂", "聊到上班摸鱼、犯困或想躺平时使用"),
            (("赞", "强", "厉害", "牛", "666", "开智"), "点赞吹捧", "夸奖群友或惊叹操作时使用"),
            (("吃", "火锅", "烧烤", "饿", "干饭"), "干饭嘴馋", "群里聊美食或约饭时使用"),
        ]
        matched_tags: list[str] = []
        matched_hints: list[str] = []
        for cues, tag, hint in cue_map:
            if any(c in context_blob for c in cues):
                matched_tags.append(tag)
                matched_hints.append(hint)

        if matched_tags:
            asset.emotion_tags = list(dict.fromkeys([t for t in (asset.emotion_tags or []) if t != "fun"] + matched_tags))
            asset.context_hint = "；".join(matched_hints[:2])
        elif context_blob and not asset.context_hint:
            asset.context_hint = f"群聊语境：「{context_blob[:60]}」时发出"
        await session.flush()
        return asset


    @staticmethod
    def extract_tags_from_text(text: str) -> list[str]:
        """Extract lightweight Chinese emotion/meme tags from a description or caption."""
        keyword_map = (
            ("大笑", ("大笑", "笑死", "哈哈", "爆笑", "乐", "幽默")),
            ("震撼", ("震撼", "震惊", "离谱", "惊呆", "懵")),
            ("无语", ("无语", "无奈", "尴尬", "翻白眼", "摊手", "叹气")),
            ("可爱", ("可爱", "猫咪", "喵", "萌", "乖巧", "贴贴")),
            ("疑惑", ("疑惑", "困惑", "好奇", "问号", "不解", "思考")),
            ("开心", ("开心", "高兴", "快乐", "激动", "点赞", "棒")),
            ("委屈", ("委屈", "哭", "难过", "伤心", "泪", "悲伤")),
            ("生气", ("生气", "愤怒", "暴躁", "怒", "打人", "气死")),
            ("摸鱼", ("摸鱼", "摆烂", "躺平", "睡觉", "困", "疲惫")),
            ("吃瓜", ("吃瓜", "围观", "看戏", "八卦")),
            ("吐槽", ("吐槽", "嘲讽", "阴阳", "滑稽", "狗头")),
        )
        found: list[str] = []
        for tag, cues in keyword_map:
            if any(c in text for c in cues):
                found.append(tag)
        return found[:4] or ["斗图"]

    async def import_from_preset(
        self,
        session: AsyncSession,
        preset_id: str | None = None,
        custom_url: str | None = None,
        limit: int = 80,
        fetcher=None,
    ) -> dict[str, Any]:
        """Import tagged stickers from a user-supplied JSON/JSONL URL (or fetcher in tests)."""
        import httpx as _httpx

        target_url = (custom_url or "").strip()
        if not target_url and fetcher is None:
            raise ValueError("请先前往推荐仓库自行下载数据集文件后通过本地上传导入，或填写可直接访问的 JSON/JSONL 地址")
        if not target_url:
            target_url = f"preset://{preset_id or 'custom'}"

        if fetcher is not None:
            raw_text = await fetcher(target_url)
        else:
            async with _httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
                resp = await client.get(target_url, headers={"User-Agent": "EchoSea-StickerImporter/1.4"})
                resp.raise_for_status()
                raw_text = resp.text
        return await self.import_from_raw_text(
            session=session,
            raw_text=raw_text,
            limit=limit,
            source_label=preset_id or "custom_url",
        )

    async def import_from_raw_text(
        self,
        session: AsyncSession,
        raw_text: str,
        limit: int = 80,
        source_label: str = "local_upload",
    ) -> dict[str, Any]:
        """Parse local uploaded JSON/JSONL sticker dataset text, auto-extract tags, deduplicate, and import."""
        import json as _json

        limit = max(1, min(500, int(limit or 80)))
        preset = None
        preset_id = source_label

        # Parse standard JSON (array or wrapped object) first; fall back to multi-line JSONL on JSONDecodeError
        records: list[dict[str, Any]] = []
        stripped = raw_text.strip()
        if stripped:
            parsed = None
            try:
                parsed = _json.loads(stripped)
            except _json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                records = [r for r in parsed if isinstance(r, dict)]
            elif isinstance(parsed, dict):
                for key in ("data", "items", "stickers", "records"):
                    if isinstance(parsed.get(key), list):
                        records = [r for r in parsed[key] if isinstance(r, dict)]
                        break
                if not records and any(k in parsed for k in ("url", "source_url", "image_url", "img")):
                    records = [parsed]
            else:
                for line in stripped.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = _json.loads(line)
                        if isinstance(obj, dict):
                            records.append(obj)
                    except Exception as exc:
                        logger.debug("Skipping malformed JSONL sticker line: %s", exc)
                        continue

        # Normalize diverse dataset schemas -> import_manual items
        normalized: list[dict[str, Any]] = []
        for rec in records:
            if len(normalized) >= limit:
                break
            url = str(rec.get("url") or rec.get("source_url") or rec.get("image_url") or rec.get("img") or "").strip()
            if not url or not (url.startswith("http://") or url.startswith("https://")):
                continue
            tags_raw = rec.get("emotion_tags") or rec.get("tags") or rec.get("category") or rec.get("label") or []
            if isinstance(tags_raw, str):
                tags = [t.strip() for t in tags_raw.replace("，", ",").split(",") if t.strip()]
            elif isinstance(tags_raw, list):
                tags = [str(t).strip() for t in tags_raw if str(t).strip()]
            else:
                tags = []
            hint = str(rec.get("context_hint") or rec.get("description") or rec.get("caption") or rec.get("text") or rec.get("name") or "").strip()[:240]
            if not tags and hint:
                tags = self.extract_tags_from_text(hint)
            normalized.append({
                "source_url": url,
                "emotion_tags": tags[:6] or ["斗图"],
                "context_hint": hint or f"来自公开预设源 {preset['name'] if preset else 'custom'}",
            })

        # Deduplicate against existing source_urls in DB
        existing_urls = set(
            (await session.execute(select(StickerAsset.source_url).where(StickerAsset.source_url.is_not(None)))).scalars().all()
        )
        fresh = [it for it in normalized if it["source_url"] not in existing_urls]
        created = await self.import_manual(session, fresh)
        return {
            "ok": True,
            "preset_id": preset_id or "custom",
            "fetched_count": len(records),
            "imported_count": len(created),
            "skipped_duplicates": len(normalized) - len(fresh),
        }

PUBLIC_STICKER_PRESETS: list[dict[str, Any]] = [
    {
        "id": "emo_visual_data",
        "name": "Emo Visual Data（GLM-4V 标注中文表情包）",
        "repo_url": "https://github.com/LLM-Red-Team/emo-visual-data",
        "description": "18,000+ 张中文表情包视觉标注数据，自带画面与情绪语义说明，最适合 AI 按语境检索斗图。",
        "format_hint": "前往仓库下载 JSON/JSONL 数据文件后，在下方上传导入",
    },
    {
        "id": "chinese_meme_desc",
        "name": "Chinese Meme Description（中文表情包图文描述库）",
        "repo_url": "https://github.com/reilxlx/chinese-meme-description-dataset",
        "description": "高质量中文表情包图文描述集，含画面神态、配字与情绪语境，适合冷启动语义匹配。",
        "format_hint": "可在 Hugging Face / GitHub 下载数据集 JSONL 后上传导入",
    },
    {
        "id": "chinese_bqb",
        "name": "ChineseBQB（GitHub 经典中文表情包合集）",
        "repo_url": "https://github.com/zhaoolee/ChineseBQB",
        "description": "按主题分类的经典中文表情包合集，适合作为群聊冷启动图库，导入后可配合 AI 自动补全语境标签。",
        "format_hint": "下载仓库索引 JSON 或挑选图片 URL 列表后在下方导入",
    },
]
