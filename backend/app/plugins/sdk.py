"""Standard Plugin Hook Interface, PluginManager, and Built-in Plugins (Auto-Sticker & Remote Backup Sync).

New capabilities (image generation, mini-games, IoT hooks, etc.) implement CompanionPlugin
and attach cleanly via PluginManager without touching core pipeline code.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from app.core.event_bus import PluginEventBus
from app.core.multimodal_service import resolve_image_phash
from app.models.db import BackupRecord, StickerAsset

logger = logging.getLogger(__name__)


class CompanionPlugin(ABC):
    name: str = "unnamed_plugin"
    version: str = "1.0.0"
    description: str = ""
    timeout_seconds: float = 5.0

    @abstractmethod
    def register(self, bus: PluginEventBus, runtime: Any) -> None:
        """Subscribe handlers onto the central PluginEventBus."""


def _is_sticker_candidate(item: Any) -> bool:
    """Filter out explicit regular photos/screenshots (sub_type=0) while accepting mface & stickers."""
    if not isinstance(item, dict) or item.get("type") not in ("image", "mface") or not item.get("url"):
        return False
    if item.get("type") == "mface":
        return True
    sub_type = item.get("sub_type", item.get("subType"))
    if sub_type is not None and str(sub_type).strip() != "":
        summary = str(item.get("summary") or "")
        fname = str(item.get("file") or "").lower()
        return str(sub_type).strip() == "1" or "表情" in summary or fname.endswith(".gif")
    return True


class AutoStickerReactionPlugin(CompanionPlugin):
    """Monitors group reactions after images to automatically steal high-heat stickers."""

    name = "auto_sticker_reaction"
    version = "1.1.0"
    description = "监测群聊图片后的正向反应（哈哈/草/偷了）异步触发 dHash 去重偷图入库"
    timeout_seconds = 5.0

    def register(self, bus: PluginEventBus, runtime: Any) -> None:
        bg_tasks: set[asyncio.Task[Any]] = set()

        async def _tag_asset_async(asset_id: int, surrounding: list[str]) -> None:
            try:
                async with runtime.db_manager.session_factory() as tag_session:
                    asset = await tag_session.get(StickerAsset, asset_id)
                    if asset is None:
                        return
                    await runtime.sticker_service.auto_tag_with_context(
                        session=tag_session,
                        asset=asset,
                        surrounding_messages=surrounding,
                        model_hub=getattr(runtime, "model_hub", None),
                    )
                    await tag_session.commit()
            except Exception as exc:
                logger.debug("Async sticker auto-tagging failed for asset %s: %s", asset_id, exc)

        async def _on_evaluated(payload: dict[str, Any]) -> None:
            env = payload.get("envelope") or {}
            if env.get("scene") != "group" or not env.get("group_id"):
                return
            group_id = int(env["group_id"])
            to_tag: list[tuple[int, list[str]]] = []
            async with runtime.db_manager.session_factory() as session:
                media = env.get("media_meta") or []
                from sqlalchemy import select as _sel
                from app.models.db import ChatMessage as _CM
                recent_rows = list(
                    (
                        await session.execute(
                            _sel(_CM.sender_display_name, _CM.raw_text)
                            .where(_CM.scene == "group", _CM.group_id == group_id, _CM.is_recalled.is_(False))
                            .order_by(_CM.id.desc())
                            .limit(5)
                        )
                    ).all()
                )
                surrounding = [f"{r[0]}: {r[1]}" for r in reversed(recent_rows) if r[1]]
                for item in media:
                    if _is_sticker_candidate(item):
                        fetcher = getattr(getattr(runtime, "multimodal_service", None), "image_fetcher", None)
                        phash, img_bytes = await resolve_image_phash(str(item["url"]), timeout=2.5, fetcher=fetcher)
                        asset = await runtime.sticker_service.observe_group_image(
                            session=session,
                            group_id=group_id,
                            image_bytes=img_bytes,
                            phash=phash,
                            source_url=str(item["url"]),
                            context_hint=str(env.get("raw_text") or ""),
                        )
                        if asset.id is not None and (not asset.context_hint or asset.emotion_tags == ["fun"]):
                            to_tag.append((asset.id, surrounding))
                raw_text = str(env.get("raw_text") or "")
                if raw_text:
                    await runtime.sticker_service.observe_group_reaction_text(session, group_id, raw_text)
                await session.commit()

            for asset_id, ctx_lines in to_tag:
                task = asyncio.create_task(_tag_asset_async(asset_id, ctx_lines), name=f"sticker_tag_{asset_id}")
                bg_tasks.add(task)
                task.add_done_callback(bg_tasks.discard)

        bus.subscribe("on_message_evaluated", self.name, _on_evaluated, timeout_seconds=self.timeout_seconds)


class RemoteBackupSyncPlugin(CompanionPlugin):
    """Hooks backup verification events & records remote sync (rclone / WebDAV / OSS) status with failure alerts."""

    name = "remote_backup_sync"
    version = "1.0.0"
    description = "记录每日数据库备份与异地网盘/OSS同步状态，同步失败自动推送告警"
    timeout_seconds = 10.0

    def register(self, bus: PluginEventBus, runtime: Any) -> None:
        async def _on_backup_requested(payload: dict[str, Any]) -> None:
            filename = str(payload.get("filename") or "companion_backup.sql.gz")
            remote_target = str(payload.get("remote_target") or "aliyun_webdav")
            backup_path = payload.get("file_path")
            size_bytes = Path(backup_path).stat().st_size if (backup_path and Path(backup_path).exists()) else 0
            failed_err = payload.get("simulated_error")

            async with runtime.db_manager.session_factory() as session:
                rec = BackupRecord(
                    filename=filename,
                    remote_target=remote_target,
                    size_bytes=size_bytes,
                    status="failed" if failed_err else "success",
                    error_message=str(failed_err) if failed_err else None,
                )
                session.add(rec)
                if failed_err:
                    await runtime.alert_service.notify(
                        session=session,
                        severity="critical",
                        category="backup_failure",
                        title="数据库异地备份同步失败",
                        detail=f"目标：{remote_target}，文件：{filename}，错误：{failed_err}",
                        bypass_cooldown=True,
                    )
                await session.commit()

        bus.subscribe("on_backup_requested", self.name, _on_backup_requested, timeout_seconds=self.timeout_seconds)


class PluginManager:
    def __init__(self, bus: PluginEventBus) -> None:
        self.bus = bus
        self._plugins: dict[str, CompanionPlugin] = {}

    def load(self, plugin: CompanionPlugin, runtime: Any) -> None:
        plugin.register(self.bus, runtime)
        self._plugins[plugin.name] = plugin
        logger.info("Loaded CompanionPlugin: %s v%s", plugin.name, plugin.version)

    def list_loaded(self) -> list[dict[str, Any]]:
        return [
            {
                "name": p.name,
                "version": p.version,
                "description": p.description,
                "timeout_seconds": p.timeout_seconds,
            }
            for p in self._plugins.values()
        ]
