"""SnowLuma OneBot v11 Gateway Adapter.

Supports:
- Reverse WebSocket (`/ws/onebot/v11`) where SnowLuma connects to Companion System.
- Forward WebSocket client where Companion System connects to SnowLuma (`ws://snowluma:3001/`).
- HTTP API fallback (`http://snowluma:3000/send_group_msg`, etc.).
- Message normalization (array segments & CQ codes), reply quote auto-expansion,
  group/friend recall interception, and Outbox delivery auditing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx
from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ChatMessage, OutboxDelivery
from app.models.schemas import MessageEnvelope

logger = logging.getLogger(__name__)

CQ_AT_RE = re.compile(r"\[CQ:at,qq=(\d+|all)[^\]]*\]")
CQ_REPLY_RE = re.compile(r"\[CQ:reply,id=(-?\d+)[^\]]*\]")
CQ_IMAGE_RE = re.compile(r"\[CQ:image,[^\]]*url=([^,\]]+)[^\]]*\]")
CQ_ANY_RE = re.compile(r"\[CQ:[^\]]+\]")


class SnowLumaAdapter:
    """Decoupled OneBot v11 protocol adapter tailored for SnowLuma gateway."""

    def __init__(
        self,
        ws_url: str = "ws://snowluma:3001/",
        http_url: str = "http://snowluma:3000",
        access_token: str = "",
        bot_qq: int = 0,
        http_access_token: str = "",
    ) -> None:
        self.ws_url = ws_url
        self.http_url = http_url.rstrip("/")
        self.access_token = access_token
        self.http_access_token = http_access_token
        self.bot_qq = bot_qq

        self._reverse_ws: WebSocket | None = None
        self._forward_ws: Any = None
        self._connected_since: datetime | None = None
        self._last_heartbeat: datetime | None = None
        self._pending_echoes: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.sent_log: deque[dict[str, Any]] = deque(maxlen=200)
        self._dispatch_tasks: set[asyncio.Task[Any]] = set()

    @property
    def is_connected(self) -> bool:
        return self._reverse_ws is not None or self._forward_ws is not None

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "gateway": "SnowLuma (OneBot v11)",
            "connected": self.is_connected,
            "mode": "reverse_ws" if self._reverse_ws else ("forward_ws" if self._forward_ws else "disconnected"),
            "bot_qq": self.bot_qq,
            "connected_since": self._connected_since.isoformat() if self._connected_since else None,
            "last_heartbeat": self._last_heartbeat.isoformat() if self._last_heartbeat else None,
        }

    def attach_reverse_ws(self, ws: WebSocket) -> None:
        self._reverse_ws = ws
        self._connected_since = datetime.now(timezone.utc)
        self._last_heartbeat = self._connected_since

    def detach_reverse_ws(self) -> None:
        self._reverse_ws = None

    def record_heartbeat(self) -> None:
        self._last_heartbeat = datetime.now(timezone.utc)

    def resolve_echo(self, data: dict[str, Any]) -> bool:
        echo = data.get("echo")
        if echo and echo in self._pending_echoes:
            fut = self._pending_echoes.pop(echo)
            if not fut.done():
                fut.set_result(data)
            return True
        return False

    async def _send_http(self, action: str, params: dict[str, Any]) -> None:
        headers = {"Authorization": f"Bearer {self.http_access_token or self.access_token}"}
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(f"{self.http_url}/{action}", headers=headers, json=params)
            response.raise_for_status()
            result = response.json()
        if result.get("retcode") != 0 or (result.get("data") or {}).get("message_id") is None:
            raise RuntimeError(f"SnowLuma rejected {action}: retcode={result.get('retcode')}")
        logger.info("SnowLuma accepted %s: message_id=%s", action, result["data"]["message_id"])

    async def _send(self, action: str, params: dict[str, Any], echo: str) -> str:
        frame = json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False)
        if self.http_url and self.http_access_token:
            await self._send_http(action, params)
            return "accepted"
        if self._reverse_ws is not None:
            await self._reverse_ws.send_text(frame)
            return "submitted"
        if self._forward_ws is not None:
            await self._forward_ws.send(frame)
            return "submitted"
        if self.http_url.startswith(("http://", "https://")):
            await self._send_http(action, params)
            return "accepted"
        return "unverified"

    async def normalize_event(
        self,
        raw_event: dict[str, Any],
        session: AsyncSession | None = None,
    ) -> MessageEnvelope | None:
        """Convert SnowLuma OneBot v11 message event into canonical MessageEnvelope."""
        if raw_event.get("post_type") != "message":
            return None

        msg_type = raw_event.get("message_type")
        if msg_type not in ("group", "private"):
            return None

        self_qq = int(raw_event.get("self_id") or self.bot_qq or 0)
        if self_qq and not self.bot_qq:
            self.bot_qq = self_qq

        sender = raw_event.get("sender") or {}
        sender_qq = int(raw_event.get("user_id") or sender.get("user_id") or 0)
        if sender_qq == self_qq and self_qq > 0:
            return None  # Ignore self-echo messages

        message_id = str(raw_event.get("message_id") or uuid.uuid4().hex[:12])
        group_id = int(raw_event["group_id"]) if msg_type == "group" and raw_event.get("group_id") else None

        mentioned_qqs: list[int] = []
        mentions_all = False
        reply_to_msg_id: str | None = None
        media_meta: list[dict[str, Any]] = []
        text_parts: list[str] = []

        message_field = raw_event.get("message")
        if isinstance(message_field, list):
            for seg in message_field:
                seg_type = seg.get("type")
                seg_data = seg.get("data") or {}
                if seg_type == "text":
                    text_parts.append(str(seg_data.get("text", "")))
                elif seg_type == "at":
                    qq_val = str(seg_data.get("qq", ""))
                    if qq_val == "all":
                        mentions_all = True
                    elif qq_val.isdigit():
                        mentioned_qqs.append(int(qq_val))
                elif seg_type == "reply":
                    if seg_data.get("id") is not None:
                        reply_to_msg_id = str(seg_data["id"])
                elif seg_type in ("image", "mface", "face"):
                    media_meta.append({"type": seg_type, **seg_data})
        else:
            raw_str = str(raw_event.get("raw_message") or message_field or "")
            for m in CQ_AT_RE.finditer(raw_str):
                if m.group(1) == "all":
                    mentions_all = True
                else:
                    mentioned_qqs.append(int(m.group(1)))
            rm = CQ_REPLY_RE.search(raw_str)
            if rm:
                reply_to_msg_id = rm.group(1)
            for im in CQ_IMAGE_RE.finditer(raw_str):
                media_meta.append({"type": "image", "url": im.group(1)})
            clean = CQ_ANY_RE.sub("", raw_str)
            text_parts.append(clean)

        raw_text = "".join(text_parts).strip()

        # Auto-expand quoted message content from DB if reply_to_msg_id is present
        expanded_reply_text: str | None = None
        quoted_text: str | None = None
        quoted_sender_qq: int | None = None
        if reply_to_msg_id and session is not None:
            scope = ChatMessage.group_id == group_id if msg_type == "group" else ChatMessage.sender_qq == sender_qq
            stmt = select(ChatMessage).where(
                ChatMessage.message_id == reply_to_msg_id,
                ChatMessage.scene == msg_type,
                ChatMessage.is_recalled.is_(False),
                scope,
            ).limit(1)
            quoted = (await session.execute(stmt)).scalar_one_or_none()
            if quoted is not None:
                quoted_text = quoted.raw_text
                quoted_sender_qq = quoted.sender_qq
                expanded_reply_text = f"{quoted.sender_display_name}: {quoted_text}"

        return MessageEnvelope(
            message_id=message_id,
            scene=msg_type,  # type: ignore[arg-type]
            group_id=group_id,
            sender_qq=sender_qq,
            sender_nickname=str(sender.get("nickname") or ""),
            sender_card=str(sender.get("card") or ""),
            raw_text=raw_text,
            reply_to_msg_id=reply_to_msg_id,
            expanded_reply_text=expanded_reply_text,
            quoted_text=quoted_text,
            quoted_sender_qq=quoted_sender_qq,
            mentioned_qqs=list(dict.fromkeys(mentioned_qqs)),
            mentions_all=mentions_all,
            media_meta=media_meta,
            self_qq=self_qq,
        )

    async def send_message(
        self,
        session: AsyncSession,
        scene: str,
        target_id: int,
        text: str,
        reply_to_msg_id: str | None = None,
    ) -> OutboxDelivery:
        """Send message via SnowLuma OneBot v11 WS/HTTP and record Outbox audit entry."""
        echo = f"out_{uuid.uuid4().hex[:12]}"
        delivery = OutboxDelivery(
            scene=scene,
            target_id=target_id,
            reply_to_msg_id=reply_to_msg_id,
            payload_text=text,
            status="pending",
            onebot_echo=echo,
        )
        session.add(delivery)
        await session.flush()

        segments: list[dict[str, Any]] = []
        if reply_to_msg_id:
            segments.append({"type": "reply", "data": {"id": str(reply_to_msg_id)}})
        segments.append({"type": "text", "data": {"text": text}})

        action = "send_group_msg" if scene == "group" else "send_private_msg"
        params = (
            {"group_id": target_id, "message": segments}
            if scene == "group"
            else {"user_id": target_id, "message": segments}
        )
        try:
            delivery.onebot_status = await self._send(action, params, echo)
            delivery.status = "sent"
            delivery.sent_at = datetime.now(timezone.utc)
        except Exception as exc:
            delivery.onebot_status = "failed"
            delivery.status = "failed"
            delivery.error_message = str(exc)
            logger.warning("SnowLuma send_message failed: %s", exc)

        self.sent_log.append(
            {
                "scene": scene,
                "target_id": target_id,
                "reply_to_msg_id": reply_to_msg_id,
                "text": text,
                "status": delivery.status,
            }
        )
        await session.flush()
        return delivery

    async def run_forward_ws_loop(
        self,
        on_event: Callable[[dict[str, Any]], Awaitable[None]],
        stop_event: asyncio.Event,
    ) -> None:
        """Background reconnect loop when operating in Forward WS client mode against SnowLuma."""
        import websockets

        backoff = 2
        while not stop_event.is_set():
            try:
                headers = {}
                if self.access_token:
                    headers["Authorization"] = f"Bearer {self.access_token}"
                async with websockets.connect(self.ws_url, additional_headers=headers, ping_interval=20, ping_timeout=20) as ws:
                    self._forward_ws = ws
                    self._connected_since = datetime.now(timezone.utc)
                    self._last_heartbeat = self._connected_since
                    backoff = 2
                    logger.info("Connected to SnowLuma Forward WS at %s", self.ws_url)
                    async for raw in ws:
                        if stop_event.is_set():
                            break
                        data = json.loads(raw)
                        if self.resolve_echo(data):
                            continue
                        if data.get("post_type") == "meta_event":
                            self.record_heartbeat()
                            continue
                        # Non-blocking dispatch so slow LLM/gatekeeper calls never stall WS keepalive or subsequent messages
                        task = asyncio.create_task(self._safe_dispatch(on_event, data), name="snowluma_forward_event")
                        self._dispatch_tasks.add(task)
                        task.add_done_callback(self._dispatch_tasks.discard)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("SnowLuma Forward WS disconnected (%s); retrying in %ss", exc, backoff)
                self._forward_ws = None
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 30)
        self._forward_ws = None


    async def send_voice(
        self,
        session: AsyncSession,
        scene: str,
        target_id: int,
        audio_b64: str,
    ) -> OutboxDelivery:
        """Send synthesized Doubao TTS audio as OneBot v11 record segment via SnowLuma."""
        echo = f"tts_{uuid.uuid4().hex[:12]}"
        delivery = OutboxDelivery(
            scene=scene,
            target_id=target_id,
            payload_text="[voice:record]",
            status="pending",
            onebot_echo=echo,
        )
        session.add(delivery)
        await session.flush()

        segments = [{"type": "record", "data": {"file": f"base64://{audio_b64}"}}]
        action = "send_group_msg" if scene == "group" else "send_private_msg"
        params = (
            {"group_id": target_id, "message": segments}
            if scene == "group"
            else {"user_id": target_id, "message": segments}
        )
        try:
            delivery.onebot_status = await self._send(action, params, echo)
            delivery.status = "sent"
            delivery.sent_at = datetime.now(timezone.utc)
        except Exception as exc:
            delivery.onebot_status = "failed"
            delivery.status = "failed"
            delivery.error_message = str(exc)
        self.sent_log.append({"scene": scene, "target_id": target_id, "type": "voice", "status": delivery.status})
        await session.flush()
        return delivery

    async def send_sticker(
        self,
        session: AsyncSession,
        scene: str,
        target_id: int,
        sticker_url_or_path: str,
    ) -> OutboxDelivery:
        """Send a sticker image segment via SnowLuma OneBot v11."""
        echo = f"stk_{uuid.uuid4().hex[:12]}"
        delivery = OutboxDelivery(
            scene=scene,
            target_id=target_id,
            payload_text=f"[sticker:{sticker_url_or_path}]",
            status="pending",
            onebot_echo=echo,
        )
        session.add(delivery)
        await session.flush()

        segments = [{"type": "image", "data": {"file": sticker_url_or_path}}]
        action = "send_group_msg" if scene == "group" else "send_private_msg"
        params = (
            {"group_id": target_id, "message": segments}
            if scene == "group"
            else {"user_id": target_id, "message": segments}
        )
        try:
            delivery.onebot_status = await self._send(action, params, echo)
            delivery.status = "sent"
            delivery.sent_at = datetime.now(timezone.utc)
        except Exception as exc:
            delivery.onebot_status = "failed"
            delivery.status = "failed"
            delivery.error_message = str(exc)
        self.sent_log.append({"scene": scene, "target_id": target_id, "type": "sticker", "file": sticker_url_or_path, "status": delivery.status})
        await session.flush()
        return delivery


    @staticmethod
    async def _safe_dispatch(on_event: Callable[[dict[str, Any]], Awaitable[None]], data: dict[str, Any]) -> None:
        try:
            await on_event(data)
        except Exception as exc:
            logger.exception("Async SnowLuma event handler failed: %s", exc)


    async def drain_tasks(self, timeout: float = 5.0) -> None:
        """Await any active Forward WS event tasks during graceful shutdown."""
        pending = [t for t in self._dispatch_tasks if not t.done()]
        if not pending:
            return
        _, still_pending = await asyncio.wait(pending, timeout=max(0.1, timeout))
        for task in still_pending:
            task.cancel()
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)
