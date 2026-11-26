"""Offsite Backup Service for EchoSea (回声海) v1.4.0:
- Supports Aliyun Drive OpenAPI (OAuth2 code exchange / Refresh Token -> auto access_token refresh + custom folder path upload)
- Supports Aliyun Drive WebDAV / AList / generic WebDAV (MKCOL + PUT to custom path)
- Supports local snapshot fallback
- Stores all credentials AES-GCM encrypted inside SystemConfig (zero DB migration required).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import local_now
from app.models.db import (
    BackupRecord,
    GroupExpression,
    HotTopicSource,
    MemoryEpisode,
    PersonaVersion,
    QQIdentity,
    StickerAsset,
    SystemConfig,
)
from app.security import decrypt_secret, encrypt_secret, mask_secret

logger = logging.getLogger(__name__)

BACKUP_CONFIG_SCOPE_TYPE = "global"
BACKUP_CONFIG_SCOPE_ID = "backup_target"
ALIYUN_OPENAPI_BASE = "https://openapi.alipan.com"


class BackupService:
    """Manages offsite backup configuration, Aliyun Drive OAuth/WebDAV upload, and BackupRecord auditing."""

    def __init__(
        self,
        master_key: str,
        local_backup_dir: str = "./backups",
        http_client_factory: Callable[[], Any] | None = None,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        self.master_key = master_key
        self.local_backup_dir = Path(local_backup_dir)
        self._http_client_factory = http_client_factory
        self.timezone_name = timezone_name

    def _make_client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        if self._http_client_factory is not None:
            return self._http_client_factory()
        return httpx.AsyncClient(timeout=timeout)

    async def _get_raw_config(self, session: AsyncSession) -> dict[str, Any]:
        stmt = select(SystemConfig).where(
            SystemConfig.scope_type == BACKUP_CONFIG_SCOPE_TYPE,
            SystemConfig.scope_id == BACKUP_CONFIG_SCOPE_ID,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None or not isinstance(row.config_json, dict):
            return {
                "provider": "aliyun_openapi",  # aliyun_openapi | aliyun_webdav | local_only
                "remote_path": "/EchoSea/backups",
                "retention_count": 14,
                "auto_enabled": False,
                "schedule_interval_hours": 24,
                "last_auto_backup_at": None,
                "aliyun_client_id": "",
                "aliyun_client_secret_enc": "",
                "aliyun_refresh_token_enc": "",
                "aliyun_drive_type": "backup",  # backup | resource
                "aliyun_oauth_proxy_url": "",  # optional custom OAuth/token refresh endpoint
                "webdav_url": "",
                "webdav_username": "",
                "webdav_password_enc": "",
            }
        return dict(row.config_json)

    async def get_safe_config(self, session: AsyncSession) -> dict[str, Any]:
        raw = await self._get_raw_config(session)
        rt_plain = self._safe_decrypt(raw.get("aliyun_refresh_token_enc", ""))
        cs_plain = self._safe_decrypt(raw.get("aliyun_client_secret_enc", ""))
        wd_plain = self._safe_decrypt(raw.get("webdav_password_enc", ""))
        return {
            "provider": raw.get("provider", "aliyun_openapi"),
            "remote_path": raw.get("remote_path", "/EchoSea/backups"),
            "retention_count": int(raw.get("retention_count", 14)),
            "auto_enabled": bool(raw.get("auto_enabled", False)),
            "schedule_interval_hours": int(raw.get("schedule_interval_hours", 24)),
            "last_auto_backup_at": raw.get("last_auto_backup_at"),
            "aliyun_client_id": raw.get("aliyun_client_id", ""),
            "aliyun_client_secret_masked": mask_secret(cs_plain) if cs_plain else "",
            "aliyun_refresh_token_masked": mask_secret(rt_plain) if rt_plain else "",
            "aliyun_authorized": bool(rt_plain),
            "aliyun_drive_type": raw.get("aliyun_drive_type", "backup"),
            "aliyun_oauth_proxy_url": raw.get("aliyun_oauth_proxy_url", ""),
            "webdav_url": raw.get("webdav_url", ""),
            "webdav_username": raw.get("webdav_username", ""),
            "webdav_password_masked": mask_secret(wd_plain) if wd_plain else "",
            "webdav_configured": bool(raw.get("webdav_url")),
        }

    async def save_config(self, session: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
        current = await self._get_raw_config(session)
        for key in (
            "provider",
            "remote_path",
            "retention_count",
            "auto_enabled",
            "schedule_interval_hours",
            "last_auto_backup_at",
            "aliyun_client_id",
            "aliyun_drive_type",
            "aliyun_oauth_proxy_url",
            "webdav_url",
            "webdav_username",
        ):
            if key in payload and payload[key] is not None:
                val = payload[key]
                if key == "remote_path":
                    val = "/" + str(val).strip().strip("/") if str(val).strip() else "/EchoSea/backups"
                elif key == "webdav_url":
                    val = str(val).strip().rstrip("/")
                elif key == "schedule_interval_hours":
                    val = max(1, min(168, int(val)))
                elif key == "retention_count":
                    val = max(1, min(90, int(val)))
                current[key] = val

        # Encrypt sensitive secrets only when non-empty (blank preserves existing secret)
        if payload.get("aliyun_client_secret"):
            current["aliyun_client_secret_enc"] = encrypt_secret(str(payload["aliyun_client_secret"]).strip(), self.master_key)
        if payload.get("aliyun_refresh_token"):
            current["aliyun_refresh_token_enc"] = encrypt_secret(str(payload["aliyun_refresh_token"]).strip(), self.master_key)
        if payload.get("webdav_password"):
            current["webdav_password_enc"] = encrypt_secret(str(payload["webdav_password"]).strip(), self.master_key)

        stmt = select(SystemConfig).where(
            SystemConfig.scope_type == BACKUP_CONFIG_SCOPE_TYPE,
            SystemConfig.scope_id == BACKUP_CONFIG_SCOPE_ID,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            session.add(
                SystemConfig(
                    scope_type=BACKUP_CONFIG_SCOPE_TYPE,
                    scope_id=BACKUP_CONFIG_SCOPE_ID,
                    config_json=current,
                )
            )
        else:
            row.config_json = current
        await session.flush()
        return await self.get_safe_config(session)

    def _safe_decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ""
        try:
            return decrypt_secret(ciphertext, self.master_key)
        except Exception:
            return ""

    async def build_aliyun_oauth_url(self, session: AsyncSession, redirect_uri: str = "oob") -> dict[str, Any]:
        raw = await self._get_raw_config(session)
        client_id = str(raw.get("aliyun_client_id") or "").strip()
        if not client_id:
            return {
                "ok": False,
                "error": "请先填写阿里云盘开放平台 Client ID（或直接使用已获取的 Refresh Token / WebDAV 模式）",
            }
        params = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": "user:base,file:all:read,file:all:write",
                "response_type": "code",
            }
        )
        return {
            "ok": True,
            "authorize_url": f"{ALIYUN_OPENAPI_BASE}/oauth/authorize?{params}",
        }

    async def exchange_aliyun_code(self, session: AsyncSession, code: str) -> dict[str, Any]:
        raw = await self._get_raw_config(session)
        client_id = str(raw.get("aliyun_client_id") or "").strip()
        client_secret = self._safe_decrypt(raw.get("aliyun_client_secret_enc", ""))
        if not client_id or not client_secret:
            return {"ok": False, "error": "缺少阿里云盘 Client ID 或 Client Secret"}
        async with self._make_client(20.0) as client:
            resp = await client.post(
                f"{ALIYUN_OPENAPI_BASE}/oauth/access_token",
                json={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "authorization_code",
                    "code": code.strip(),
                },
            )
            resp.raise_for_status()
            data = resp.json()
        refresh_token = str(data.get("refresh_token") or "").strip()
        if not refresh_token:
            return {"ok": False, "error": f"授权交换未返回 refresh_token: {data}"}
        safe = await self.save_config(session, {"aliyun_refresh_token": refresh_token})
        return {"ok": True, "config": safe}

    async def _refresh_aliyun_access_token(self, session: AsyncSession, raw: dict[str, Any]) -> str:
        refresh_token = self._safe_decrypt(raw.get("aliyun_refresh_token_enc", ""))
        if not refresh_token:
            raise RuntimeError("尚未配置阿里云盘 Refresh Token")
        proxy_url = str(raw.get("aliyun_oauth_proxy_url") or "").strip()
        client_id = str(raw.get("aliyun_client_id") or "").strip()
        client_secret = self._safe_decrypt(raw.get("aliyun_client_secret_enc", ""))

        token_url = proxy_url or f"{ALIYUN_OPENAPI_BASE}/oauth/access_token"
        body: dict[str, Any] = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        if client_id:
            body["client_id"] = client_id
        if client_secret:
            body["client_secret"] = client_secret

        async with self._make_client(20.0) as client:
            resp = await client.post(token_url, json=body)
            resp.raise_for_status()
            data = resp.json()
        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError(f"刷新阿里云盘 access_token 失败: {data}")
        new_rt = str(data.get("refresh_token") or "").strip()
        if new_rt and new_rt != refresh_token:
            await self.save_config(session, {"aliyun_refresh_token": new_rt})
        return access_token

    async def test_connection(self, session: AsyncSession) -> dict[str, Any]:
        raw = await self._get_raw_config(session)
        provider = raw.get("provider", "aliyun_openapi")
        try:
            if provider == "local_only":
                self.local_backup_dir.mkdir(parents=True, exist_ok=True)
                return {"ok": True, "provider": provider, "message": f"本地备份目录可用：{self.local_backup_dir}"}
            if provider == "aliyun_webdav":
                url = str(raw.get("webdav_url") or "").strip()
                if not url:
                    return {"ok": False, "provider": provider, "error": "请先填写 WebDAV 服务地址"}
                user = str(raw.get("webdav_username") or "")
                pwd = self._safe_decrypt(raw.get("webdav_password_enc", ""))
                auth = (user, pwd) if user else None
                async with self._make_client(15.0) as client:
                    resp = await client.request("OPTIONS", f"{url}/", auth=auth)
                    if resp.status_code >= 400:
                        resp = await client.request("PROPFIND", f"{url}/", headers={"Depth": "0"}, auth=auth)
                    resp.raise_for_status()
                return {"ok": True, "provider": provider, "message": f"WebDAV 连接成功，目标路径：{raw.get('remote_path', '/EchoSea/backups')}"}

            # aliyun_openapi
            access_token = await self._refresh_aliyun_access_token(session, raw)
            async with self._make_client(15.0) as client:
                resp = await client.post(
                    f"{ALIYUN_OPENAPI_BASE}/adrive/v1.0/user/getDriveInfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json={},
                )
                resp.raise_for_status()
                info = resp.json()
            drive_id = info.get("backup_drive_id") or info.get("default_drive_id") or info.get("resource_drive_id")
            return {
                "ok": True,
                "provider": provider,
                "message": f"阿里云盘授权有效（Drive ID: {drive_id}），目标路径：{raw.get('remote_path', '/EchoSea/backups')}",
            }
        except Exception as exc:
            return {"ok": False, "provider": provider, "error": str(exc)}

    async def _build_snapshot_bytes(self, session: AsyncSession) -> tuple[str, bytes]:
        """Build a portable, privacy-preserving gzipped JSON snapshot of EchoSea core state."""
        identities = list((await session.execute(select(QQIdentity))).scalars().all())
        episodes = list((await session.execute(select(MemoryEpisode))).scalars().all())
        expressions = list((await session.execute(select(GroupExpression))).scalars().all())
        stickers = list((await session.execute(select(StickerAsset))).scalars().all())
        personas = list((await session.execute(select(PersonaVersion))).scalars().all())
        hot_sources = list((await session.execute(select(HotTopicSource))).scalars().all())

        snapshot = {
            "project": "EchoSea",
            "exported_at": local_now(self.timezone_name).isoformat(),
            "counts": {
                "identities": len(identities),
                "episodes": len(episodes),
                "expressions": len(expressions),
                "stickers": len(stickers),
                "personas": len(personas),
                "hot_sources": len(hot_sources),
            },
            "identities": [
                {
                    "qq_id": i.qq_id,
                    "nickname": i.nickname,
                    "group_cards": i.group_cards,
                    "known_names": i.known_names,
                    "relationship_level": i.relationship_level,
                    "public_profile": i.public_profile,
                    "private_profile": i.private_profile,
                }
                for i in identities
            ],
            "episodes": [
                {
                    "scope": e.scope,
                    "group_id": e.group_id,
                    "qq_id": e.qq_id,
                    "title": e.title,
                    "content": e.content,
                    "importance": e.importance,
                }
                for e in episodes
            ],
            "expressions": [
                {"group_id": x.group_id, "phrase": x.phrase, "meaning": x.meaning, "status": x.status}
                for x in expressions
            ],
            "stickers": [
                {
                    "phash": s.phash,
                    "source_url": s.source_url,
                    "emotion_tags": s.emotion_tags,
                    "context_hint": s.context_hint,
                    "status": s.status,
                }
                for s in stickers
            ],
            "personas": [
                {
                    "version_tag": p.version_tag,
                    "bot_name": p.bot_name,
                    "aliases": p.aliases,
                    "system_prompt": p.system_prompt,
                    "banned_phrases": p.banned_phrases,
                    "is_active": p.is_active,
                }
                for p in personas
            ],
            "hot_sources": [
                {"name": h.name, "category": h.category, "feed_url": h.feed_url, "is_enabled": h.is_enabled}
                for h in hot_sources
            ],
        }
        ts = local_now(self.timezone_name).strftime("%Y%m%d_%H%M%S")
        filename = f"echosea_backup_{ts}.json.gz"
        payload = gzip.compress(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))
        return filename, payload

    async def _upload_webdav(self, raw: dict[str, Any], filename: str, data: bytes) -> str:
        base_url = str(raw.get("webdav_url") or "").strip().rstrip("/")
        if not base_url:
            raise RuntimeError("未配置 WebDAV 地址")
        remote_path = "/" + str(raw.get("remote_path") or "/EchoSea/backups").strip().strip("/")
        user = str(raw.get("webdav_username") or "")
        pwd = self._safe_decrypt(raw.get("webdav_password_enc", ""))
        auth = (user, pwd) if user else None

        async with self._make_client(60.0) as client:
            # Ensure nested directories exist via MKCOL
            acc = ""
            for seg in [s for s in remote_path.split("/") if s]:
                acc += f"/{quote(seg)}"
                r = await client.request("MKCOL", f"{base_url}{acc}", auth=auth)
                if r.status_code not in (200, 201, 204, 301, 405, 409):
                    r.raise_for_status()
            target_url = f"{base_url}{acc}/{quote(filename)}"
            put_resp = await client.put(target_url, content=data, auth=auth, headers={"Content-Type": "application/gzip"})
            put_resp.raise_for_status()
        return f"aliyun_webdav:{remote_path}/{filename}"

    async def _upload_aliyun_openapi(self, session: AsyncSession, raw: dict[str, Any], filename: str, data: bytes) -> str:
        access_token = await self._refresh_aliyun_access_token(session, raw)
        headers = {"Authorization": f"Bearer {access_token}"}
        remote_path = "/" + str(raw.get("remote_path") or "/EchoSea/backups").strip().strip("/")
        drive_type = str(raw.get("aliyun_drive_type") or "backup")

        async with self._make_client(60.0) as client:
            info_resp = await client.post(f"{ALIYUN_OPENAPI_BASE}/adrive/v1.0/user/getDriveInfo", headers=headers, json={})
            info_resp.raise_for_status()
            info = info_resp.json()
            drive_id = (
                info.get("resource_drive_id") if drive_type == "resource" else info.get("backup_drive_id")
            ) or info.get("default_drive_id")
            if not drive_id:
                raise RuntimeError(f"无法获取阿里云盘 drive_id: {info}")

            # Recursively ensure custom folder path exists
            parent_id = "root"
            for seg in [s for s in remote_path.split("/") if s]:
                mk = await client.post(
                    f"{ALIYUN_OPENAPI_BASE}/adrive/v1.0/openFile/create",
                    headers=headers,
                    json={
                        "drive_id": drive_id,
                        "parent_file_id": parent_id,
                        "name": seg,
                        "type": "folder",
                        "check_name_mode": "refuse",
                    },
                )
                mk.raise_for_status()
                mk_data = mk.json()
                parent_id = str(mk_data.get("file_id") or parent_id)

            # Create file + pre-signed upload URL + complete
            sha1_hex = hashlib.sha1(data).hexdigest().upper()
            create_resp = await client.post(
                f"{ALIYUN_OPENAPI_BASE}/adrive/v1.0/openFile/create",
                headers=headers,
                json={
                    "drive_id": drive_id,
                    "parent_file_id": parent_id,
                    "name": filename,
                    "type": "file",
                    "check_name_mode": "auto_rename",
                    "size": len(data),
                    "content_hash_name": "sha1",
                    "content_hash": sha1_hex,
                    "part_info_list": [{"part_number": 1}],
                },
            )
            create_resp.raise_for_status()
            cdata = create_resp.json()
            if not cdata.get("rapid_upload"):
                parts = cdata.get("part_info_list") or []
                if parts and parts[0].get("upload_url"):
                    up = await client.put(parts[0]["upload_url"], content=data)
                    up.raise_for_status()
                comp = await client.post(
                    f"{ALIYUN_OPENAPI_BASE}/adrive/v1.0/openFile/complete",
                    headers=headers,
                    json={
                        "drive_id": drive_id,
                        "file_id": cdata["file_id"],
                        "upload_id": cdata.get("upload_id", ""),
                    },
                )
                comp.raise_for_status()
        return f"aliyun_openapi:{remote_path}/{filename}"

    @staticmethod
    def _sanitize_filename(custom_filename: str) -> str:
        name = str(custom_filename).strip()
        if not name or "/" in name or "\\" in name or ".." in name or "\x00" in name or name.startswith("."):
            raise ValueError("非法备份文件名：禁止包含路径分隔符或相对路径")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name):
            raise ValueError("非法备份文件名：仅允许字母、数字、点、下划线与短横线（1-128 字符）")
        return name

    async def run_backup(self, session: AsyncSession, custom_filename: str | None = None) -> BackupRecord:
        raw = await self._get_raw_config(session)
        provider = raw.get("provider", "local_only")
        filename, data = await self._build_snapshot_bytes(session)
        if custom_filename:
            filename = self._sanitize_filename(custom_filename)

        # Always keep a local copy first, strictly confined to local_backup_dir
        self.local_backup_dir.mkdir(parents=True, exist_ok=True)
        base_dir = self.local_backup_dir.resolve()
        dest_path = (base_dir / filename).resolve()
        if dest_path.parent != base_dir:
            raise ValueError("非法备份文件名：禁止写出备份目录")
        dest_path.write_bytes(data)

        status = "success"
        remote_target = f"local:{self.local_backup_dir / filename}"
        err_msg: str | None = None
        try:
            if provider == "aliyun_openapi":
                remote_target = await self._upload_aliyun_openapi(session, raw, filename, data)
            elif provider == "aliyun_webdav":
                remote_target = await self._upload_webdav(raw, filename, data)
        except Exception as exc:
            status = "failed"
            remote_target = f"{provider}:{raw.get('remote_path', '/EchoSea/backups')}/{filename}"
            err_msg = str(exc)
            logger.warning("Offsite backup upload failed (%s): %s", provider, exc)

        rec = BackupRecord(
            filename=filename,
            remote_target=remote_target,
            size_bytes=len(data),
            status=status,
            error_message=err_msg,
        )
        session.add(rec)
        await session.flush()
        return rec

    def cleanup_old_local_backups(self, retention_count: int = 14) -> int:
        """Keep only the newest `retention_count` local snapshot files."""
        if not self.local_backup_dir.exists():
            return 0
        files = sorted(
            self.local_backup_dir.glob("echosea_backup_*.json.gz"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for stale in files[max(1, int(retention_count)):]:
            try:
                stale.unlink()
                removed += 1
            except Exception as exc:
                logger.warning("Failed removing stale backup %s: %s", stale, exc)
        return removed

    async def run_scheduled_if_due(self, session: AsyncSession, alert_service=None) -> BackupRecord | None:
        """Execute an automatic backup when auto_enabled is True and the configured interval has elapsed."""
        raw = await self._get_raw_config(session)
        if not raw.get("auto_enabled"):
            return None
        interval_hours = max(1, int(raw.get("schedule_interval_hours", 24)))
        now = datetime.now(timezone.utc)
        last_str = raw.get("last_auto_backup_at")
        if last_str:
            try:
                last_dt = datetime.fromisoformat(str(last_str))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if (now - last_dt).total_seconds() < interval_hours * 3600:
                    return None
            except Exception as exc:
                logger.warning("Invalid last_auto_backup_at '%s': %s", last_str, exc)

        rec = await self.run_backup(session)
        self.cleanup_old_local_backups(int(raw.get("retention_count", 14)))
        await self.save_config(session, {"last_auto_backup_at": now.isoformat()})
        if rec.status == "failed" and alert_service is not None:
            await alert_service.notify(
                session,
                severity="critical",
                category="backup_failure",
                title=f"定时异地备份失败：{rec.filename}",
                detail=f"目标：{rec.remote_target}，原因：{rec.error_message or '未知错误'}",
            )
        return rec

    @staticmethod
    def _parse_snapshot_bytes(raw_bytes: bytes) -> dict[str, Any]:
        """Decompress (if gzipped) and validate an EchoSea backup snapshot payload."""
        if not raw_bytes:
            raise ValueError("备份文件为空")
        payload_bytes = raw_bytes
        if raw_bytes[:2] == b"\x1f\x8b":
            try:
                payload_bytes = gzip.decompress(raw_bytes)
            except Exception as exc:
                raise ValueError(f"Gzip 解压失败：{exc}") from exc
        try:
            data = json.loads(payload_bytes.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"快照 JSON 解析失败：{exc}") from exc
        if not isinstance(data, dict) or data.get("project") != "EchoSea":
            raise ValueError("无效的 EchoSea 快照格式（缺少 project=EchoSea 标识）")
        for key in ("identities", "episodes", "expressions", "stickers", "personas", "hot_sources"):
            if key in data and not isinstance(data[key], list):
                raise ValueError(f"快照字段 {key} 格式错误")
        return data

    async def verify_and_restore(
        self,
        session: AsyncSession,
        raw_bytes: bytes,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Validate a gzipped/plain EchoSea JSON snapshot and atomically restore it when dry_run=False."""
        data = self._parse_snapshot_bytes(raw_bytes)
        identities = data.get("identities") or []
        episodes = data.get("episodes") or []
        expressions = data.get("expressions") or []
        stickers = data.get("stickers") or []
        personas = data.get("personas") or []
        hot_sources = data.get("hot_sources") or []
        summary = {
            "identities": len(identities),
            "episodes": len(episodes),
            "expressions": len(expressions),
            "stickers": len(stickers),
            "personas": len(personas),
            "hot_sources": len(hot_sources),
        }
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "exported_at": data.get("exported_at"),
                "counts": summary,
            }

        restored = {k: 0 for k in summary}
        # Upsert identities first so episode FKs stay valid
        for item in identities:
            qq_id = int(item["qq_id"])
            row = await session.get(QQIdentity, qq_id)
            if row is None:
                row = QQIdentity(qq_id=qq_id)
                session.add(row)
            row.nickname = str(item.get("nickname") or row.nickname or "")
            row.group_cards = dict(item.get("group_cards") or {})
            row.known_names = list(item.get("known_names") or [])
            row.relationship_level = int(item.get("relationship_level") or 0)
            row.public_profile = dict(item.get("public_profile") or {})
            row.private_profile = dict(item.get("private_profile") or {})
            restored["identities"] += 1

        await session.flush()

        for item in personas:
            vtag = str(item.get("version_tag") or "").strip()
            if not vtag:
                continue
            existing = (await session.execute(select(PersonaVersion).where(PersonaVersion.version_tag == vtag))).scalar_one_or_none()
            if existing is None:
                session.add(
                    PersonaVersion(
                        version_tag=vtag,
                        bot_name=str(item.get("bot_name") or "小呆呆"),
                        aliases=list(item.get("aliases") or []),
                        system_prompt=str(item.get("system_prompt") or ""),
                        banned_phrases=list(item.get("banned_phrases") or []),
                        is_active=bool(item.get("is_active", False)),
                    )
                )
            else:
                existing.bot_name = str(item.get("bot_name") or existing.bot_name)
                existing.aliases = list(item.get("aliases") or [])
                existing.system_prompt = str(item.get("system_prompt") or existing.system_prompt)
                existing.banned_phrases = list(item.get("banned_phrases") or [])
                existing.is_active = bool(item.get("is_active", existing.is_active))
            restored["personas"] += 1

        for item in expressions:
            gid = int(item["group_id"])
            phrase = str(item.get("phrase") or "").strip()
            if not phrase:
                continue
            existing = (
                await session.execute(
                    select(GroupExpression).where(GroupExpression.group_id == gid, GroupExpression.phrase == phrase)
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    GroupExpression(
                        group_id=gid,
                        phrase=phrase,
                        meaning=str(item.get("meaning") or ""),
                        status=str(item.get("status") or "pending"),
                    )
                )
            else:
                existing.meaning = str(item.get("meaning") or existing.meaning)
                existing.status = str(item.get("status") or existing.status)
            restored["expressions"] += 1

        for item in stickers:
            phash = str(item.get("phash") or "").strip()
            if not phash:
                continue
            existing = (await session.execute(select(StickerAsset).where(StickerAsset.phash == phash))).scalar_one_or_none()
            if existing is None:
                session.add(
                    StickerAsset(
                        phash=phash,
                        source_url=str(item.get("source_url") or ""),
                        emotion_tags=list(item.get("emotion_tags") or []),
                        context_hint=str(item.get("context_hint") or ""),
                        status=str(item.get("status") or "pending"),
                    )
                )
            else:
                existing.source_url = str(item.get("source_url") or existing.source_url)
                existing.emotion_tags = list(item.get("emotion_tags") or existing.emotion_tags or [])
                existing.context_hint = str(item.get("context_hint") or existing.context_hint)
                existing.status = str(item.get("status") or existing.status)
            restored["stickers"] += 1

        for item in hot_sources:
            name = str(item.get("name") or "").strip()
            feed_url = str(item.get("feed_url") or "").strip()
            if not feed_url:
                continue
            existing = (await session.execute(select(HotTopicSource).where(HotTopicSource.feed_url == feed_url))).scalar_one_or_none()
            if existing is None:
                session.add(
                    HotTopicSource(
                        name=name or feed_url[:40],
                        category=str(item.get("category") or "general"),
                        feed_url=feed_url,
                        is_enabled=bool(item.get("is_enabled", True)),
                    )
                )
                restored["hot_sources"] += 1

        existing_ep_keys = set(
            (await session.execute(select(MemoryEpisode.scope, MemoryEpisode.group_id, MemoryEpisode.qq_id, MemoryEpisode.title))).all()
        )
        for item in episodes:
            key = (str(item.get("scope") or "public_group"), item.get("group_id"), item.get("qq_id"), str(item.get("title") or ""))
            if key in existing_ep_keys:
                continue
            session.add(
                MemoryEpisode(
                    scope=key[0],
                    group_id=key[1],
                    qq_id=key[2],
                    title=key[3],
                    content=str(item.get("content") or ""),
                    importance=float(item.get("importance") or 0.5),
                )
            )
            existing_ep_keys.add(key)
            restored["episodes"] += 1

        await session.flush()
        return {
            "ok": True,
            "dry_run": False,
            "exported_at": data.get("exported_at"),
            "counts": summary,
            "restored": restored,
        }
