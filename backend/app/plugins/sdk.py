"""Standard Plugin Hook Interface, PluginManager, and Built-in Plugins (Auto-Sticker & Remote Backup Sync).

New capabilities (image generation, mini-games, IoT hooks, etc.) implement CompanionPlugin
and attach cleanly via PluginManager without touching core pipeline code.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from app.core.event_bus import PluginEventBus
from app.models.db import BackupRecord

logger = logging.getLogger(__name__)


class CompanionPlugin(ABC):
    name: str = "unnamed_plugin"
    version: str = "1.0.0"
    description: str = ""
    timeout_seconds: float = 5.0

    @abstractmethod
    def register(self, bus: PluginEventBus, runtime: Any) -> None:
        """Subscribe handlers onto the central PluginEventBus."""


class AutoStickerReactionPlugin(CompanionPlugin):
    """Monitors group reactions after images to automatically steal high-heat stickers."""

    name = "auto_sticker_reaction"
    version = "1.0.0"
    description = "监测群聊图片后的正向反应（哈哈/草/偷了）自动触发偷图入库"
    timeout_seconds = 3.0

    def register(self, bus: PluginEventBus, runtime: Any) -> None:
        async def _on_evaluated(payload: dict[str, Any]) -> None:
            env = payload.get("envelope") or {}
            if env.get("scene") != "group" or not env.get("group_id"):
                return
            group_id = int(env["group_id"])
            async with runtime.db_manager.session_factory() as session:
                media = env.get("media_meta") or []
                for item in media:
                    if item.get("type") in ("image", "mface") and item.get("url"):
                        await runtime.sticker_service.observe_group_image(
                            session=session,
                            group_id=group_id,
                            phash=f"url_{abs(hash(item['url'])) % (10**16):016x}",
                            source_url=str(item["url"]),
                            context_hint=str(env.get("raw_text") or ""),
                        )
                raw_text = str(env.get("raw_text") or "")
                if raw_text:
                    await runtime.sticker_service.observe_group_reaction_text(session, group_id, raw_text)
                await session.commit()

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
