"""Doubao (Volcengine) TTS Voice Pipeline (v1.1):
- Separates `synthesis_status` (synthesized / fallback_text) from `onebot_status` (pending / accepted / failed).
- Correlates with unified `request_id` and OneBot `onebot_echo` receipt.
"""
from __future__ import annotations

import base64
import logging
import re
import uuid
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ModelProvider, TTSDelivery
from app.security import decrypt_secret

logger = logging.getLogger(__name__)

EMOTION_SPEED_MAP = {
    "excited": 1.15,
    "happy": 1.08,
    "relaxed": 1.0,
    "neutral": 1.0,
    "sleepy": 0.88,
    "sad": 0.9,
    "angry": 1.12,
}

URL_RE = re.compile(r"https?://\S+")
PAREN_ACTION_RE = re.compile(r"[（(][^）)]{1,24}[）)]|\[[^\]]{1,24}\]")
MD_RE = re.compile(r"[*_`#~>]+")


def detect_voice_intent(text: str, has_reply_quote: bool = False) -> str:
    """Classify user's explicit voice instruction into: force_text | read_quote | answer_voice | default."""
    t = text.strip()
    if any(k in t for k in ("别发语音", "不要发语音", "用文字", "打字说", "别用语音")):
        return "force_text"
    if has_reply_quote and any(k in t for k in ("用语音", "读一下", "念一下", "朗读")):
        return "read_quote"
    if any(k in t for k in ("用语音回答", "发语音", "语音说", "语音回复")):
        return "answer_voice"
    return "default"


def clean_text_for_tts(text: str) -> str:
    """Strip Markdown, links, and parenthetical stage directions before voice synthesis."""
    cleaned = URL_RE.sub("", text)
    cleaned = PAREN_ACTION_RE.sub("", cleaned)
    cleaned = MD_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


class DoubaoTTSService:
    """Synthesizes speech via Doubao TTS slot in Model Control Center with automatic text fallback."""

    def __init__(
        self,
        master_key: str,
        max_duration_seconds: float = 45.0,
        synthesizer: Callable[[str, dict[str, Any], float], Awaitable[tuple[bytes, float]]] | None = None,
    ) -> None:
        self.master_key = master_key
        self.max_duration_seconds = max_duration_seconds
        self._synthesizer = synthesizer

    async def synthesize_and_record(
        self,
        session: AsyncSession,
        scene: str,
        target_id: int,
        text: str,
        intent: str = "answer_voice",
        emotion: str = "neutral",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        cleaned = clean_text_for_tts(text)
        speed = EMOTION_SPEED_MAP.get(emotion, 1.0)
        est_duration = round(max(1.0, len(cleaned) / (4.2 * speed)), 2)

        delivery = TTSDelivery(
            request_id=request_id,
            scene=scene,
            target_id=target_id,
            intent=intent,
            cleaned_text=cleaned,
            emotion=emotion,
            speed_ratio=speed,
            duration_seconds=est_duration,
            synthesis_status="pending",
            onebot_status="pending",
            status="pending",
        )
        session.add(delivery)
        await session.flush()

        if not cleaned or est_duration > self.max_duration_seconds:
            delivery.synthesis_status = "fallback_text"
            delivery.onebot_status = "skipped"
            delivery.status = "fallback_text"
            delivery.fallback_reason = (
                "empty_cleaned_text" if not cleaned else f"exceeded_max_duration_{self.max_duration_seconds}s"
            )
            await session.flush()
            return {"ok": False, "delivery_id": delivery.id, "fallback_text": text, "reason": delivery.fallback_reason}

        stmt = (
            select(ModelProvider)
            .where(ModelProvider.slot == "tts", ModelProvider.is_enabled.is_(True))
            .order_by(ModelProvider.role.desc())
        )
        provider = (await session.execute(stmt)).scalars().first()
        if provider is None and self._synthesizer is None:
            delivery.synthesis_status = "fallback_text"
            delivery.onebot_status = "skipped"
            delivery.status = "fallback_text"
            delivery.fallback_reason = "tts_provider_not_configured"
            await session.flush()
            return {"ok": False, "delivery_id": delivery.id, "fallback_text": text, "reason": delivery.fallback_reason}

        try:
            if self._synthesizer is not None:
                audio_bytes, actual_duration = await self._synthesizer(cleaned, {"emotion": emotion}, speed)
            else:
                assert provider is not None
                token = decrypt_secret(provider.api_key_enc, self.master_key)
                audio_bytes, actual_duration = await self._call_doubao_api(provider, token, cleaned, emotion, speed)

            if actual_duration > self.max_duration_seconds:
                delivery.synthesis_status = "fallback_text"
                delivery.onebot_status = "skipped"
                delivery.status = "fallback_text"
                delivery.fallback_reason = f"audio_too_long_{actual_duration:.1f}s"
                await session.flush()
                return {"ok": False, "delivery_id": delivery.id, "fallback_text": text, "reason": delivery.fallback_reason}

            delivery.duration_seconds = actual_duration
            delivery.synthesis_status = "synthesized"
            delivery.onebot_status = "pending"
            delivery.status = "synthesized"
            await session.flush()
            return {
                "ok": True,
                "delivery_id": delivery.id,
                "audio_b64": base64.b64encode(audio_bytes).decode("ascii"),
                "duration_seconds": actual_duration,
                "cleaned_text": cleaned,
                "synthesis_status": "synthesized",
            }
        except Exception as exc:
            logger.warning("Doubao TTS failed, falling back to text: %s", exc)
            delivery.synthesis_status = "failed"
            delivery.onebot_status = "skipped"
            delivery.status = "fallback_text"
            delivery.fallback_reason = str(exc)
            await session.flush()
            return {"ok": False, "delivery_id": delivery.id, "fallback_text": text, "reason": str(exc)}

    async def mark_onebot_status(
        self,
        session: AsyncSession,
        delivery_id: int,
        onebot_echo: str,
        onebot_status: str,
    ) -> None:
        """Record 3-state OneBot delivery status (submitted | accepted | failed) separately from synthesis."""
        row = await session.get(TTSDelivery, delivery_id)
        if row is not None:
            row.onebot_echo = onebot_echo
            row.onebot_status = onebot_status
            row.status = "sent" if onebot_status in ("submitted", "accepted") else "failed"
            await session.flush()

    async def mark_onebot_receipt(
        self,
        session: AsyncSession,
        delivery_id: int,
        onebot_echo: str,
        accepted: bool,
    ) -> None:
        """Record OneBot acceptance status separately from TTS synthesis status."""
        row = await session.get(TTSDelivery, delivery_id)
        if row is not None:
            row.onebot_echo = onebot_echo
            row.onebot_status = "accepted" if accepted else "failed"
            row.status = "sent" if accepted else "failed"
            await session.flush()

    @staticmethod
    async def _call_doubao_api(
        provider: ModelProvider,
        api_key: str,
        cleaned_text: str,
        emotion: str,
        speed: float,
    ) -> tuple[bytes, float]:
        extra = provider.extra_config or {}
        payload = {
            "app": {
                "appid": extra.get("appid", ""),
                "token": api_key,
                "cluster": extra.get("cluster", "volcano_tts"),
            },
            "user": {"uid": "companion_bot"},
            "audio": {
                "voice_type": provider.model_name or extra.get("voice_type", "BV700_V2_streaming"),
                "encoding": "mp3",
                "speed_ratio": speed,
                "emotion": emotion,
            },
            "request": {
                "reqid": uuid.uuid4().hex,
                "text": cleaned_text,
                "operation": "query",
            },
        }
        headers = {"Authorization": f"Bearer;{api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=provider.timeout_seconds) as client:
            resp = await client.post(provider.base_url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            audio_b64 = data.get("data", "")
            if not audio_b64:
                raise RuntimeError(f"Doubao TTS returned no audio data: {data.get('message', 'unknown')}")
            audio_bytes = base64.b64decode(audio_b64)
            duration = float(data.get("addition", {}).get("duration", 0)) / 1000.0
            if duration <= 0:
                duration = round(max(1.0, len(cleaned_text) / (4.2 * speed)), 2)
            return audio_bytes, duration
