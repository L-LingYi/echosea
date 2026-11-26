"""Smart Circuit-Breaker Cooldown & Half-Open Auto-Recovery for Model Providers."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_service import AlertService
from app.models.db import ModelProvider

logger = logging.getLogger(__name__)


class ProviderCircuitBreaker:
    """Tracks provider failures, trips smart cooldowns, and recovers primaries automatically."""

    def __init__(self, alert_service: AlertService, clock: Callable[[], float] | None = None) -> None:
        self.alert_service = alert_service
        self._clock = clock or time.time
        self.provider_failures: dict[int, int] = {}
        self.cooldowns: dict[int, dict[str, Any]] = {}

    def now(self) -> float:
        return float(self._clock())

    def get_cooldown_state(self, provider_id: int) -> dict[str, Any]:
        info = self.cooldowns.get(provider_id)
        if not info:
            return {
                "cooldown_active": False,
                "cooldown_remaining_seconds": 0,
                "cooldown_until": None,
                "cooldown_reason": None,
                "cooldown_kind": None,
            }
        remaining = int(max(0.0, info["cooldown_until"] - self.now()))
        active = remaining > 0
        return {
            "cooldown_active": active,
            "cooldown_remaining_seconds": remaining,
            "cooldown_until": datetime.fromtimestamp(info["cooldown_until"], tz=timezone.utc).isoformat() if active else None,
            "cooldown_reason": info.get("reason") if active else None,
            "cooldown_kind": info.get("kind") if active else None,
        }

    def clear_cooldown(self, provider_id: int) -> bool:
        existed = provider_id in self.cooldowns
        self.cooldowns.pop(provider_id, None)
        self.provider_failures.pop(provider_id, None)
        return existed

    @staticmethod
    def classify_provider_error(exc: Exception) -> tuple[str, int | None, str]:
        """Classify failure into ('quota_exhausted' | 'auth_or_config' | 'transient', retry_after_sec, detail)."""
        status_code: int | None = None
        retry_after: int | None = None
        body_text = ""

        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            status_code = exc.response.status_code
            ra = exc.response.headers.get("Retry-After") or exc.response.headers.get("retry-after")
            if ra:
                try:
                    retry_after = max(30, min(86400, int(float(ra.strip()))))
                except Exception:
                    retry_after = None
            try:
                body_text = (exc.response.text or "")[:300]
            except Exception:
                body_text = ""

        msg = f"{type(exc).__name__}: {exc} {body_text}".lower()
        quota_keywords = (
            "quota", "rate limit", "rate_limit", "too many requests",
            "insufficient", "balance", "credit", "limit exceeded", "exhausted", "free tier",
        )

        if status_code in (429, 402) or any(k in msg for k in quota_keywords):
            detail = f"HTTP {status_code} 额度/限流触发冷却" if status_code else f"额度/限流触发冷却 ({type(exc).__name__})"
            return "quota_exhausted", retry_after, detail
        if status_code in (400, 401, 403, 404):
            return "auth_or_config", None, f"HTTP {status_code} 配置/鉴权异常"
        return "transient", retry_after, f"上游暂不可用 ({type(exc).__name__})"

    def compute_cooldown_seconds(
        self,
        provider: ModelProvider,
        kind: str,
        strike_count: int,
        retry_after: int | None,
    ) -> int:
        extra = provider.extra_config or {}
        if kind == "quota_exhausted":
            if retry_after is not None:
                return retry_after
            if "quota_reset_utc_hour" in extra and extra["quota_reset_utc_hour"] is not None:
                try:
                    from datetime import timedelta

                    target_hour = int(extra["quota_reset_utc_hour"]) % 24
                    now_dt = datetime.fromtimestamp(self.now(), tz=timezone.utc)
                    next_reset = now_dt.replace(hour=target_hour, minute=0, second=0, microsecond=0)
                    if next_reset <= now_dt:
                        next_reset += timedelta(days=1)
                    return max(60, int((next_reset - now_dt).total_seconds()))
                except Exception as exc:
                    logger.debug("Invalid quota_reset_utc_hour '%s': %s", extra.get("quota_reset_utc_hour"), exc)
            base_sec = int(extra.get("quota_cooldown_seconds", 3600))
            return min(21600, max(60, base_sec * (2 ** min(strike_count - 1, 3))))
        if kind == "auth_or_config":
            base_sec = int(extra.get("error_cooldown_seconds", 1800))
            return min(14400, max(60, base_sec * (2 ** min(strike_count - 1, 2))))
        base_sec = int(extra.get("transient_cooldown_seconds", 300))
        return min(1800, max(30, base_sec * (2 ** min(strike_count - 1, 3))))

    async def record_failure(
        self,
        session: AsyncSession,
        provider: ModelProvider,
        exc: Exception,
        has_alternative: bool,
    ) -> None:
        kind, retry_after, detail = self.classify_provider_error(exc)
        pid = provider.id
        self.provider_failures[pid] = self.provider_failures.get(pid, 0) + 1
        fail_count = self.provider_failures[pid]

        should_trip = (kind in ("quota_exhausted", "auth_or_config")) or (fail_count >= 3)
        if not should_trip:
            return

        prev = self.cooldowns.get(pid) or {}
        strikes = int(prev.get("strike_count", 0)) + 1
        cooldown_sec = self.compute_cooldown_seconds(provider, kind, strikes, retry_after)
        until_ts = self.now() + cooldown_sec
        self.cooldowns[pid] = {
            "cooldown_until": until_ts,
            "reason": detail,
            "kind": kind,
            "strike_count": strikes,
            "tripped_at": datetime.fromtimestamp(self.now(), tz=timezone.utc).isoformat(),
        }
        provider.last_health_status = "cooldown"
        provider.last_checked_at = datetime.now(timezone.utc)
        logger.warning(
            "Provider '%s' (%s/%s) entered %ds cooldown [%s, strike=%d]: %s",
            provider.name,
            provider.slot,
            provider.role,
            cooldown_sec,
            kind,
            strikes,
            detail,
        )
        if strikes == 1 and has_alternative:
            mins = max(1, cooldown_sec // 60)
            await self.alert_service.notify(
                session,
                severity="warning" if kind != "auth_or_config" else "critical",
                category="provider_failover",
                title=f"槽位 {provider.slot} 主路冷却，已自动切至备路",
                detail=f"{provider.name} ({provider.model_name}) 触发 {detail}，进入 {mins} 分钟冷却期；到期后将自动试探恢复主路。",
            )

    def order_providers(self, providers: list[ModelProvider]) -> list[ModelProvider]:
        if len(providers) <= 1:
            return providers
        now = self.now()
        ready: list[ModelProvider] = []
        cooling: list[tuple[float, ModelProvider]] = []
        for p in providers:
            info = self.cooldowns.get(p.id)
            if info and now < float(info["cooldown_until"]):
                cooling.append((float(info["cooldown_until"]), p))
            else:
                ready.append(p)
        if ready:
            return ready + [p for _, p in sorted(cooling, key=lambda x: x[0])]
        return [p for _, p in sorted(cooling, key=lambda x: x[0])]
