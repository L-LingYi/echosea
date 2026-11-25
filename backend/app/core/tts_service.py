"""Doubao (Volcengine) TTS Voice Pipeline:
- 3 explicit user voice intents: answer_voice, read_quote, force_text.
- Single-segment merged synthesis for coherent tone.
- Pre-TTS cleaning (strips Markdown, URLs, parenthetical stage directions).
- Emotion-to-speed mapping, max duration guard, timeout fallback to text, full receipt logging.
"""
from __future__ import annotations

import base64
import json
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
    if any(k in t for k in ("用语音", "发语音", "语音说", "语音回复")):
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
    ) -> dict[str, Any]:
        cleaned = clean_text_for_tts(text)
        speed = EMOTION_SPEED_MAP.get(emotion, 1.0)
        # Estimate rough duration (~4.2 Chinese chars/sec) for duration guard
        est_duration = round(max(1.0, len(cleaned) / (4.2 * speed)), 2)

        delivery = TTSDelivery(
            scene=scene,
            target_id=target_id,
            intent=intent,
            cleaned_text=cleaned,
            emotion=emotion,
            speed_ratio=speed,
            duration_seconds=est_duration,
            status="pending",
        )
        session.add(delivery)
        await session.flush()

        if not cleaned or est_duration > self.max_duration_seconds:
            delivery.status = "fallback_text"
            delivery.fallback_reason = (
                "empty_cleaned_text" if not cleaned else f"exceeded_max_duration_{self.max_duration_seconds}s"
            )
            await session.flush()
            return {"ok": False, "delivery_id": delivery.id, "fallback_text": text, "reason": delivery.fallback_reason}

        # Look up TTS slot from Model Control Center
        stmt = (
            select(ModelProvider)
            .where(ModelProvider.slot == "tts", ModelProvider.is_enabled.is_(True))
            .order_by(ModelProvider.role.desc())
        )
        provider = (await session.execute(stmt)).scalars().first()
        if provider is None and self._synthesizer is None:
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
                delivery.status = "fallback_text"
                delivery.fallback_reason = f"audio_too_long_{actual_duration:.1f}s"
                await session.flush()
                return {"ok": False, "delivery_id": delivery.id, "fallback_text": text, "reason": delivery.fallback_reason}

            delivery.duration_seconds = actual_duration
            delivery.status = "sent"
            await session.flush()
            return {
                "ok": True,
                "delivery_id": delivery.id,
                "audio_b64": base64.b64encode(audio_bytes).decode("ascii"),
                "duration_seconds": actual_duration,
                "cleaned_text": cleaned,
            }
        except Exception as exc:
            logger.warning("Doubao TTS failed, falling back to text: %s", exc)
            delivery.status = "fallback_text"
            delivery.fallback_reason = str(exc)
            await session.flush()
            return {"ok": False, "delivery_id": delivery.id, "fallback_text": text, "reason": str(exc)}

    @staticmethod
    async def _call_doubao_api(
        provider: ModelProvider,
        api_key: str,
        cleaned_text: str,
        emotion: str,
        speed: float,
    ) -> tuple[bytes, float]:
        extra = provider.extra_config or {}
        if "/api/v3/tts/" in provider.base_url:
            headers = {
                "X-Api-Key": api_key,
                "X-Api-Resource-Id": extra.get("cluster", "seed-icl-2.0"),
                "X-Api-Request-Id": str(uuid.uuid4()),
            }
            payload = {
                "user": {"uid": "companion_bot"},
                "req_params": {
                    "text": cleaned_text,
                    "speaker": provider.model_name,
                    "audio_params": {
                        "format": "mp3",
                        "sample_rate": 24000,
                        "loudness_rate": int(extra.get("loudness_rate", 0)),
                        "speech_rate": max(-50, min(100, round((extra.get("speed_ratio", speed) - 1) * 100))),
                    },
                },
            }
            audio = bytearray()
            async with httpx.AsyncClient(timeout=provider.timeout_seconds) as client:
                async with client.stream("POST", provider.base_url, headers=headers, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        line = line.strip().removeprefix("data:").strip()
                        if line == "[DONE]":
                            break
                        if not line or line.startswith(("event:", ":")):
                            continue
                        chunk = json.loads(line)
                        if chunk.get("code", 0) not in (0, 20000000):
                            raise RuntimeError(f"Volcengine V3 TTS returned code {chunk['code']}")
                        if chunk.get("data"):
                            audio.extend(base64.b64decode(chunk["data"], validate=True))
            if not audio:
                raise RuntimeError("Volcengine V3 TTS returned no audio")
            return bytes(audio), len(cleaned_text) / (4.2 * speed)

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
