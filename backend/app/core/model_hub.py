"""Unified Model Control Center: 6 standard slots, Primary+Fallback failover, AES-GCM keys,
health probe, telemetry metrics, and Daily Budget Hard Circuit Breaker (80% alert / 100% fuse).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_service import AlertService
from app.models.db import ModelProvider, UsageMetric
from app.security import decrypt_secret, encrypt_secret, mask_secret

logger = logging.getLogger(__name__)


@dataclass
class ChatCompletionResult:
    content: str
    provider_name: str
    model_name: str
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    ttft_ms: int = 0
    total_ms: int = 0
    used_fallback: bool = False


class ModelHubService:
    """Central registry and execution router for all 6 external capability slots."""

    VALID_SLOTS = ("main_brain", "gatekeeper", "reflection", "embedding", "tts", "web_search")

    def __init__(
        self,
        master_key: str,
        alert_service: AlertService,
        daily_budget_usd: float = 5.0,
        budget_alert_ratio: float = 0.8,
        http_caller: Callable[[ModelProvider, str, list[dict[str, Any]], float, int], Awaitable[dict[str, Any]]]
        | None = None,
    ) -> None:
        self.master_key = master_key
        self.alert_service = alert_service
        self.daily_budget_usd = daily_budget_usd
        self.budget_alert_ratio = budget_alert_ratio
        self._http_caller = http_caller
        self._consecutive_failures: dict[str, int] = {}

    async def upsert_provider(self, session: AsyncSession, data: dict[str, Any]) -> ModelProvider:
        stmt = select(ModelProvider).where(
            ModelProvider.slot == data["slot"],
            ModelProvider.role == data["role"],
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        raw_key = data.get("api_key", "")

        if existing is None:
            provider = ModelProvider(
                slot=data["slot"],
                role=data["role"],
                name=data["name"],
                protocol=data.get("protocol", "openai_compatible"),
                base_url=data["base_url"].rstrip("/"),
                model_name=data["model_name"],
                api_key_enc=encrypt_secret(raw_key, self.master_key),
                extra_config=data.get("extra_config", {}),
                rate_limit_rpm=data.get("rate_limit_rpm", 60),
                timeout_seconds=data.get("timeout_seconds", 30),
                price_input_per_1m=data.get("price_input_per_1m", 0.0),
                price_output_per_1m=data.get("price_output_per_1m", 0.0),
                is_enabled=data.get("is_enabled", True),
            )
            session.add(provider)
            await session.flush()
            return provider

        existing.name = data["name"]
        existing.protocol = data.get("protocol", existing.protocol)
        existing.base_url = data["base_url"].rstrip("/")
        existing.model_name = data["model_name"]
        if raw_key:
            existing.api_key_enc = encrypt_secret(raw_key, self.master_key)
        existing.extra_config = data.get("extra_config", existing.extra_config)
        existing.rate_limit_rpm = data.get("rate_limit_rpm", existing.rate_limit_rpm)
        existing.timeout_seconds = data.get("timeout_seconds", existing.timeout_seconds)
        existing.price_input_per_1m = data.get("price_input_per_1m", existing.price_input_per_1m)
        existing.price_output_per_1m = data.get("price_output_per_1m", existing.price_output_per_1m)
        existing.is_enabled = data.get("is_enabled", existing.is_enabled)
        await session.flush()
        return existing

    def to_safe_view(self, provider: ModelProvider) -> dict[str, Any]:
        plain = ""
        try:
            plain = decrypt_secret(provider.api_key_enc, self.master_key)
        except Exception:
            plain = ""
        return {
            "id": provider.id,
            "slot": provider.slot,
            "role": provider.role,
            "name": provider.name,
            "protocol": provider.protocol,
            "base_url": provider.base_url,
            "model_name": provider.model_name,
            "api_key_masked": mask_secret(plain),
            "extra_config": provider.extra_config,
            "rate_limit_rpm": provider.rate_limit_rpm,
            "timeout_seconds": provider.timeout_seconds,
            "price_input_per_1m": provider.price_input_per_1m,
            "price_output_per_1m": provider.price_output_per_1m,
            "is_enabled": provider.is_enabled,
            "last_health_status": provider.last_health_status,
            "last_health_ms": provider.last_health_ms,
            "last_checked_at": provider.last_checked_at,
        }

    async def get_today_spend_usd(self, session: AsyncSession) -> float:
        now = datetime.now(timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        stmt = select(func.coalesce(func.sum(UsageMetric.estimated_cost_usd), 0.0)).where(
            UsageMetric.created_at >= start_of_day
        )
        total = (await session.execute(stmt)).scalar_one()
        return float(total or 0.0)

    async def is_budget_fuse_active(self, session: AsyncSession) -> bool:
        if self.daily_budget_usd <= 0:
            return False
        spend = await self.get_today_spend_usd(session)
        return spend >= self.daily_budget_usd

    async def chat_complete(
        self,
        session: AsyncSession,
        slot: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int = 400,
    ) -> ChatCompletionResult:
        """Invoke primary provider for slot; seamlessly failover to fallback provider on error."""
        stmt = (
            select(ModelProvider)
            .where(ModelProvider.slot == slot, ModelProvider.is_enabled.is_(True))
            .order_by(ModelProvider.role.desc())  # 'primary' > 'fallback' alphabetically
        )
        providers = list((await session.execute(stmt)).scalars().all())
        if not providers:
            raise RuntimeError(f"No enabled model provider configured for slot '{slot}'")

        last_err: Exception | None = None
        for idx, provider in enumerate(providers):
            api_key = decrypt_secret(provider.api_key_enc, self.master_key)
            t0 = time.perf_counter()
            try:
                if self._http_caller is not None:
                    raw = await self._http_caller(provider, api_key, messages, temperature, max_tokens)
                else:
                    raw = await self._call_openai_compatible(provider, api_key, messages, temperature, max_tokens)

                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                usage = raw.get("usage", {})
                prompt_tokens = int(usage.get("prompt_tokens", 0))
                completion_tokens = int(usage.get("completion_tokens", 0))
                cached_tokens = int(
                    usage.get("prompt_tokens_details", {}).get("cached_tokens", usage.get("cached_tokens", 0))
                )
                cost = (
                    (prompt_tokens / 1_000_000.0) * provider.price_input_per_1m
                    + (completion_tokens / 1_000_000.0) * provider.price_output_per_1m
                )

                content = ""
                choices = raw.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "") or ""

                metric = UsageMetric(
                    slot=slot,
                    provider_name=provider.name,
                    model_name=provider.model_name,
                    prompt_tokens=prompt_tokens,
                    cached_tokens=cached_tokens,
                    completion_tokens=completion_tokens,
                    estimated_cost_usd=cost,
                    ttft_ms=elapsed_ms,
                    total_ms=elapsed_ms,
                    success=True,
                )
                session.add(metric)
                await session.flush()

                self._consecutive_failures[slot] = 0
                await self._check_budget_alerts(session)

                return ChatCompletionResult(
                    content=content.strip(),
                    provider_name=provider.name,
                    model_name=provider.model_name,
                    prompt_tokens=prompt_tokens,
                    cached_tokens=cached_tokens,
                    completion_tokens=completion_tokens,
                    estimated_cost_usd=cost,
                    ttft_ms=elapsed_ms,
                    total_ms=elapsed_ms,
                    used_fallback=(idx > 0 or provider.role == "fallback"),
                )
            except Exception as exc:
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                last_err = exc
                logger.warning("Provider '%s' (%s/%s) failed: %s", provider.name, slot, provider.role, exc)
                session.add(
                    UsageMetric(
                        slot=slot,
                        provider_name=provider.name,
                        model_name=provider.model_name,
                        total_ms=elapsed_ms,
                        success=False,
                    )
                )
                await session.flush()

        # Both primary and fallback failed
        self._consecutive_failures[slot] = self._consecutive_failures.get(slot, 0) + 1
        if self._consecutive_failures[slot] >= 3:
            await self.alert_service.notify(
                session,
                severity="critical",
                category="model_api_failure",
                title=f"模型槽位 {slot} 连续报错",
                detail=f"主备路由均不可用，最近错误：{last_err}",
            )
        raise RuntimeError(f"All providers failed for slot '{slot}': {last_err}")

    async def probe_provider(self, session: AsyncSession, provider_id: int) -> dict[str, Any]:
        """One-click live health check for a configured provider slot."""
        provider = await session.get(ModelProvider, provider_id)
        if provider is None:
            raise ValueError("Provider not found")

        api_key = decrypt_secret(provider.api_key_enc, self.master_key)
        t0 = time.perf_counter()
        try:
            if self._http_caller is not None:
                res = await self._http_caller(
                    provider,
                    api_key,
                    [{"role": "user", "content": "ping"}],
                    0.0,
                    16,
                )
                reply = res.get("choices", [{}])[0].get("message", {}).get("content", "ok")
            else:
                res = await self._call_openai_compatible(
                    provider,
                    api_key,
                    [{"role": "user", "content": "Reply with OK"}],
                    0.0,
                    16,
                )
                reply = res.get("choices", [{}])[0].get("message", {}).get("content", "OK")

            ms = int((time.perf_counter() - t0) * 1000)
            provider.last_health_status = "healthy"
            provider.last_health_ms = ms
            provider.last_checked_at = datetime.now(timezone.utc)
            await session.flush()
            return {"ok": True, "latency_ms": ms, "sample_reply": str(reply)[:80]}
        except Exception as exc:
            ms = int((time.perf_counter() - t0) * 1000)
            provider.last_health_status = "error"
            provider.last_health_ms = ms
            provider.last_checked_at = datetime.now(timezone.utc)
            await session.flush()
            return {"ok": False, "latency_ms": ms, "error": str(exc)}

    async def _check_budget_alerts(self, session: AsyncSession) -> None:
        if self.daily_budget_usd <= 0:
            return
        spend = await self.get_today_spend_usd(session)
        ratio = spend / self.daily_budget_usd
        if ratio >= 1.0:
            await self.alert_service.notify(
                session,
                severity="critical",
                category="budget_fuse",
                title="日预算已达 100%，自动熔断已触发",
                detail=f"今日预估费用 ${spend:.4f} / 上限 ${self.daily_budget_usd:.2f}。系统已进入保护模式（仅响应管理员与明确 @）。",
            )
        elif ratio >= self.budget_alert_ratio:
            await self.alert_service.notify(
                session,
                severity="warning",
                category="budget_warning",
                title=f"日预算消耗已达 {ratio * 100:.0f}%",
                detail=f"今日预估费用 ${spend:.4f} / 上限 ${self.daily_budget_usd:.2f}，请留意群聊活跃度与模型路由。",
            )

    @staticmethod
    async def _call_openai_compatible(
        provider: ModelProvider,
        api_key: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        url = f"{provider.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json", **(provider.extra_config or {}).get("headers", {})}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": provider.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=provider.timeout_seconds) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()
