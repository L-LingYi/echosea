"""24h Multi-Channel Alerting Service (SMTP Email, Webhook Feishu/DingTalk/Bark, Admin QQ DM)."""
from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import time
from email.message import EmailMessage
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import AlertChannel, AlertEvent
from app.security import decrypt_secret

logger = logging.getLogger(__name__)


class AlertService:
    def __init__(self, master_key: str, cooldown_seconds: int = 300) -> None:
        self.master_key = master_key
        self.cooldown_seconds = cooldown_seconds
        self._last_sent: dict[str, float] = {}
        self.qq_sender: Callable[[int, str], Awaitable[bool]] | None = None

    def set_qq_sender(self, sender: Callable[[int, str], Awaitable[bool]]) -> None:
        self.qq_sender = sender

    async def notify(
        self,
        session: AsyncSession,
        severity: str,
        category: str,
        title: str,
        detail: str,
        bypass_cooldown: bool = False,
    ) -> AlertEvent | None:
        dedup_key = f"{category}:{title}"
        now = time.monotonic()
        if not bypass_cooldown:
            last = self._last_sent.get(dedup_key)
            if last is not None and (now - last) < self.cooldown_seconds:
                return None
        self._last_sent[dedup_key] = now

        stmt = select(AlertChannel).where(AlertChannel.is_enabled.is_(True))
        channels = list((await session.execute(stmt)).scalars().all())

        delivered: list[str] = []
        for ch in channels:
            try:
                cfg_raw = decrypt_secret(ch.config_enc, self.master_key)
                cfg = json.loads(cfg_raw) if cfg_raw else {}
                ok = await self._dispatch_channel(ch.channel_type, cfg, severity, title, detail)
                if ok:
                    delivered.append(f"{ch.channel_type}:{ch.name}")
            except Exception as exc:
                logger.warning("Alert dispatch failed on channel %s (%s): %s", ch.name, ch.channel_type, exc)

        event = AlertEvent(
            severity=severity,
            category=category,
            title=title,
            detail=detail,
            delivered_channels=delivered,
        )
        session.add(event)
        await session.flush()
        return event

    async def _dispatch_channel(
        self,
        channel_type: str,
        cfg: dict[str, Any],
        severity: str,
        title: str,
        detail: str,
    ) -> bool:
        if channel_type == "webhook":
            url = cfg.get("url", "").strip()
            if not url:
                return False
            payload = {
                "msg_type": "text",
                "title": f"[{severity.upper()}] {title}",
                "body": detail,
                "content": {"text": f"[{severity.upper()}] {title}\n{detail}"},
                "text": {"content": f"[{severity.upper()}] {title}\n{detail}"},
            }
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(url, json=payload)
                return resp.status_code < 400

        if channel_type == "smtp":
            host = cfg.get("host", "").strip()
            to_addr = cfg.get("to_addr", "").strip()
            if not host or not to_addr:
                return False
            await asyncio.to_thread(self._send_smtp_sync, cfg, severity, title, detail)
            return True

        if channel_type == "qq_admin":
            admin_qq = int(cfg.get("admin_qq", 0))
            if admin_qq > 0 and self.qq_sender is not None:
                return await self.qq_sender(admin_qq, f"[系统告警/{severity.upper()}] {title}\n{detail}")
            return False

        return False

    @staticmethod
    def _send_smtp_sync(cfg: dict[str, Any], severity: str, title: str, detail: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[Companion System][{severity.upper()}] {title}"
        msg["From"] = cfg.get("from_addr") or cfg.get("username", "noreply@companion.local")
        msg["To"] = cfg["to_addr"]
        msg.set_content(detail)

        port = int(cfg.get("port", 465))
        use_ssl = bool(cfg.get("use_ssl", port == 465))
        if use_ssl:
            with smtplib.SMTP_SSL(cfg["host"], port, timeout=10) as server:
                if cfg.get("username") and cfg.get("password"):
                    server.login(cfg["username"], cfg["password"])
                server.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], port, timeout=10) as server:
                server.starttls()
                if cfg.get("username") and cfg.get("password"):
                    server.login(cfg["username"], cfg["password"])
                server.send_message(msg)
