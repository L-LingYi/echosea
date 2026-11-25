"""Unified Model Control Center (v1.3.4):
- 6 standard slots, Primary+Fallback failover, AES-GCM encrypted keys.
- Native TypeSafe AI Jev System One (`typesafe_systemone` -> POST /v1/systemone) using official `choice` & `noul` primitives.
- Smart Circuit-Breaker Cooldown & Half-Open Auto-Recovery: skips quota-exhausted (429/402) or failing primaries during cooldown
  with 0ms overhead, then automatically probes and restores primary once the cooldown or periodic reset window elapses.
- Separates UsageMetric `source` ('production' | 'probe' | 'reflection' | 'sandbox') so probes never pollute stats.
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_service import AlertService
from app.models.db import ModelProvider, UsageMetric
from app.security import decrypt_secret, encrypt_secret, mask_secret

logger = logging.getLogger(__name__)

VALID_GATEKEEPER_TARGETS = {"bot", "other_user", "third_person_about_bot", "group_open"}


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
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw_message: dict[str, Any] = field(default_factory=dict)


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
        systemone_caller: Callable[[ModelProvider, str, str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.master_key = master_key
        self.alert_service = alert_service
        self.daily_budget_usd = daily_budget_usd
        self.budget_alert_ratio = budget_alert_ratio
        self._http_caller = http_caller
        self._systemone_caller = systemone_caller
        self._clock = clock or time.time
        self._consecutive_failures: dict[str, int] = {}
        self._provider_failures: dict[int, int] = {}
        self._cooldowns: dict[int, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Smart Cooldown / Circuit-Breaker & Half-Open Auto-Recovery (v1.3.4)
    # ------------------------------------------------------------------
    def _now(self) -> float:
        return float(self._clock())

    def get_cooldown_state(self, provider_id: int) -> dict[str, Any]:
        info = self._cooldowns.get(provider_id)
        if not info:
            return {
                "cooldown_active": False,
                "cooldown_remaining_seconds": 0,
                "cooldown_until": None,
                "cooldown_reason": None,
                "cooldown_kind": None,
            }
        remaining = int(max(0.0, info["cooldown_until"] - self._now()))
        active = remaining > 0
        return {
            "cooldown_active": active,
            "cooldown_remaining_seconds": remaining,
            "cooldown_until": datetime.fromtimestamp(info["cooldown_until"], tz=timezone.utc).isoformat() if active else None,
            "cooldown_reason": info.get("reason") if active else None,
            "cooldown_kind": info.get("kind") if active else None,
        }

    def clear_cooldown(self, provider_id: int) -> bool:
        existed = provider_id in self._cooldowns
        self._cooldowns.pop(provider_id, None)
        self._provider_failures.pop(provider_id, None)
        return existed

    @staticmethod
    def _classify_provider_error(exc: Exception) -> tuple[str, int | None, str]:
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
        quota_keywords = ("quota", "rate limit", "rate_limit", "too many requests", "insufficient", "balance", "credit", "limit exceeded", "exhausted", "free tier")

        if status_code in (429, 402) or any(k in msg for k in quota_keywords):
            detail = f"HTTP {status_code} 额度/限流触发冷却" if status_code else f"额度/限流触发冷却 ({type(exc).__name__})"
            return "quota_exhausted", retry_after, detail
        if status_code in (400, 401, 403, 404):
            return "auth_or_config", None, f"HTTP {status_code} 配置/鉴权异常"
        return "transient", retry_after, f"上游暂不可用 ({type(exc).__name__})"

    def _compute_cooldown_seconds(
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
            # Optional periodic UTC reset hour (e.g. {"quota_reset_utc_hour": 0} for daily midnight UTC reset)
            if "quota_reset_utc_hour" in extra and extra["quota_reset_utc_hour"] is not None:
                try:
                    target_hour = int(extra["quota_reset_utc_hour"]) % 24
                    now_dt = datetime.fromtimestamp(self._now(), tz=timezone.utc)
                    next_reset = now_dt.replace(hour=target_hour, minute=0, second=0, microsecond=0)
                    if next_reset <= now_dt:
                        next_reset += timedelta(days=1)
                    return max(60, int((next_reset - now_dt).total_seconds()))
                except Exception:
                    pass
            base_sec = int(extra.get("quota_cooldown_seconds", 3600))
            return min(21600, max(60, base_sec * (2 ** min(strike_count - 1, 3))))
        if kind == "auth_or_config":
            base_sec = int(extra.get("error_cooldown_seconds", 1800))
            return min(14400, max(60, base_sec * (2 ** min(strike_count - 1, 2))))
        # transient: short exponential backoff (5m -> 10m -> 20m -> 30m)
        base_sec = int(extra.get("transient_cooldown_seconds", 300))
        return min(1800, max(30, base_sec * (2 ** min(strike_count - 1, 3))))

    async def _record_provider_failure(
        self,
        session: AsyncSession,
        provider: ModelProvider,
        exc: Exception,
        has_alternative: bool,
    ) -> None:
        kind, retry_after, detail = self._classify_provider_error(exc)
        pid = provider.id
        self._provider_failures[pid] = self._provider_failures.get(pid, 0) + 1
        fail_count = self._provider_failures[pid]

        # Quota or auth/config errors trip immediately; transient errors trip after 3 consecutive failures
        should_trip = (kind in ("quota_exhausted", "auth_or_config")) or (fail_count >= 3)
        if not should_trip:
            return

        prev = self._cooldowns.get(pid) or {}
        strikes = int(prev.get("strike_count", 0)) + 1
        cooldown_sec = self._compute_cooldown_seconds(provider, kind, strikes, retry_after)
        until_ts = self._now() + cooldown_sec
        self._cooldowns[pid] = {
            "cooldown_until": until_ts,
            "reason": detail,
            "kind": kind,
            "strike_count": strikes,
            "tripped_at": datetime.fromtimestamp(self._now(), tz=timezone.utc).isoformat(),
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
        # Emit dashboard alert on first trip so Ops knows failover occurred
        if strikes == 1 and has_alternative:
            mins = max(1, cooldown_sec // 60)
            await self.alert_service.notify(
                session,
                severity="warning" if kind != "auth_or_config" else "critical",
                category="provider_failover",
                title=f"槽位 {provider.slot} 主路冷却，已自动切至备路",
                detail=f"{provider.name} ({provider.model_name}) 触发 {detail}，进入 {mins} 分钟冷却期；到期后将自动试探恢复主路。",
            )

    def _order_providers_with_cooldown(self, providers: list[ModelProvider]) -> list[ModelProvider]:
        """Skip providers currently in active cooldown when a healthy alternative exists.
        Once cooldown expires, the primary naturally returns to index 0 (half-open auto-recovery).
        """
        if len(providers) <= 1:
            return providers
        now = self._now()
        ready: list[ModelProvider] = []
        cooling: list[tuple[float, ModelProvider]] = []
        for p in providers:
            info = self._cooldowns.get(p.id)
            if info and now < float(info["cooldown_until"]):
                cooling.append((float(info["cooldown_until"]), p))
            else:
                ready.append(p)
        if ready:
            return ready + [p for _, p in sorted(cooling, key=lambda x: x[0])]
        # All cooling: try the one closest to expiry first
        return [p for _, p in sorted(cooling, key=lambda x: x[0])]

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
                extra_config=data.get("extra_config") or {},
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
        if data.get("extra_config") is not None:
            existing.extra_config = data["extra_config"]
        existing.rate_limit_rpm = data.get("rate_limit_rpm", existing.rate_limit_rpm)
        existing.timeout_seconds = data.get("timeout_seconds", existing.timeout_seconds)
        existing.price_input_per_1m = data.get("price_input_per_1m", existing.price_input_per_1m)
        existing.price_output_per_1m = data.get("price_output_per_1m", existing.price_output_per_1m)
        existing.is_enabled = data.get("is_enabled", existing.is_enabled)
        self.clear_cooldown(existing.id)
        await session.flush()
        return existing

    def to_safe_view(self, provider: ModelProvider) -> dict[str, Any]:
        plain = ""
        try:
            plain = decrypt_secret(provider.api_key_enc, self.master_key)
        except Exception:
            plain = ""
        cd = self.get_cooldown_state(provider.id)
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
            "last_health_status": "cooldown" if cd["cooldown_active"] else provider.last_health_status,
            "last_health_ms": provider.last_health_ms,
            "last_checked_at": provider.last_checked_at,
            **cd,
        }

    async def get_today_spend_usd(self, session: AsyncSession, start_of_day: datetime | None = None) -> float:
        if start_of_day is None:
            now = datetime.now(timezone.utc)
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        stmt = select(func.coalesce(func.sum(UsageMetric.estimated_cost_usd), 0.0)).where(
            UsageMetric.created_at >= start_of_day,
            UsageMetric.source != "probe",
        )
        total = (await session.execute(stmt)).scalar_one()
        return float(total or 0.0)

    async def is_budget_fuse_active(self, session: AsyncSession) -> bool:
        if self.daily_budget_usd <= 0:
            return False
        spend = await self.get_today_spend_usd(session)
        return spend >= self.daily_budget_usd

    @staticmethod
    async def _invoke_http_caller(
        caller: Callable[..., Awaitable[dict[str, Any]]],
        provider: ModelProvider,
        api_key: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        session_scope: str | None = None,
    ) -> dict[str, Any]:
        """Call injected test/custom http_caller while preserving 5-arg backwards compatibility."""
        try:
            sig = inspect.signature(caller)
            params = sig.parameters
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            extra: dict[str, Any] = {}
            if accepts_kwargs or "tools" in params:
                extra["tools"] = tools
            if accepts_kwargs or "tool_choice" in params:
                extra["tool_choice"] = tool_choice
            if accepts_kwargs or "session_scope" in params:
                extra["session_scope"] = session_scope
            return await caller(provider, api_key, messages, temperature, max_tokens, **extra)
        except (ValueError, TypeError):
            return await caller(provider, api_key, messages, temperature, max_tokens)

    async def chat_complete(
        self,
        session: AsyncSession,
        slot: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        max_tokens: int = 400,
        source: str = "production",
        request_id: str | None = None,
        session_scope: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        disable_thinking: bool = False,
    ) -> ChatCompletionResult:
        """Invoke primary provider for slot; seamlessly failover & skip cooling primaries until recovery."""
        stmt = (
            select(ModelProvider)
            .where(ModelProvider.slot == slot, ModelProvider.is_enabled.is_(True))
            .order_by(ModelProvider.role.desc())
        )
        providers = list((await session.execute(stmt)).scalars().all())
        if not providers:
            raise RuntimeError(f"No enabled model provider configured for slot '{slot}'")

        ordered = self._order_providers_with_cooldown(providers)
        last_err: Exception | None = None
        for idx, provider in enumerate(ordered):
            api_key = decrypt_secret(provider.api_key_enc, self.master_key)
            t0 = time.perf_counter()
            try:
                if self._http_caller is not None:
                    raw = await self._invoke_http_caller(
                        self._http_caller,
                        provider,
                        api_key,
                        messages,
                        temperature,
                        max_tokens,
                        tools=tools,
                        tool_choice=tool_choice,
                        session_scope=session_scope,
                    )
                else:
                    try:
                        raw = await self._call_openai_compatible(
                            provider,
                            api_key,
                            messages,
                            temperature,
                            max_tokens,
                            session_scope=session_scope,
                            tools=tools,
                            tool_choice=tool_choice,
                            disable_thinking=disable_thinking,
                        )
                    except httpx.HTTPStatusError as exc:
                        if tools and exc.response is not None and exc.response.status_code in (400, 422):
                            logger.warning(
                                "Provider '%s' rejected tools payload (HTTP %s); retrying without tools",
                                provider.name,
                                exc.response.status_code,
                            )
                            raw = await self._call_openai_compatible(
                                provider,
                                api_key,
                                messages,
                                temperature,
                                max_tokens,
                                session_scope=session_scope,
                                disable_thinking=disable_thinking,
                            )
                        else:
                            raise

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
                tool_calls: list[dict[str, Any]] = []
                raw_message: dict[str, Any] = {}
                choices = raw.get("choices", [])
                if choices:
                    msg = choices[0].get("message", {}) or {}
                    raw_message = dict(msg)
                    content = msg.get("content") or ""
                    if isinstance(msg.get("tool_calls"), list):
                        tool_calls = [tc for tc in msg["tool_calls"] if isinstance(tc, dict)]

                # Auto-clear cooldown & refresh health on live success
                was_cooling = self.clear_cooldown(provider.id)
                if was_cooling:
                    logger.info("Provider '%s' (%s/%s) recovered from cooldown!", provider.name, slot, provider.role)
                provider.last_health_status = "healthy"
                provider.last_health_ms = elapsed_ms
                provider.last_checked_at = datetime.now(timezone.utc)

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
                    source=source,
                    request_id=request_id,
                    success=True,
                )
                session.add(metric)
                await session.flush()

                self._consecutive_failures[slot] = 0
                if source == "production":
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
                    tool_calls=tool_calls,
                    raw_message=raw_message,
                )
            except Exception as exc:
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                last_err = exc
                logger.warning("Provider '%s' (%s/%s) failed: %s", provider.name, slot, provider.role, exc)
                await self._record_provider_failure(session, provider, exc, has_alternative=(len(providers) > 1))
                session.add(
                    UsageMetric(
                        slot=slot,
                        provider_name=provider.name,
                        model_name=provider.model_name,
                        total_ms=elapsed_ms,
                        source=source,
                        request_id=request_id,
                        success=False,
                    )
                )
                await session.flush()

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

    async def evaluate_gatekeeper_slot(
        self,
        session: AsyncSession,
        state_text: str,
        default_target: str,
        request_id: str | None = None,
        session_scope: str | None = None,
        timeout_override: float | None = None,
    ) -> dict[str, Any] | None:
        """Invoke configured 'gatekeeper' slot: natively supports TypeSafe AI Jev (`typesafe_systemone`)
        with official `choice` & `noul` primitives, or `openai_compatible` fallback, with smart cooldown routing.
        """
        stmt = (
            select(ModelProvider)
            .where(ModelProvider.slot == "gatekeeper", ModelProvider.is_enabled.is_(True))
            .order_by(ModelProvider.role.desc())
        )
        providers = list((await session.execute(stmt)).scalars().all())
        if not providers:
            return None

        # Official TypeSafe AI System One schema: `choice` (with `criteria` dict) + `noul`
        questions = {
            "target": {
                "type": "choice",
                "instructions": (
                    "Using the recent group chat context, classify who the latest message is addressed to. "
                    "If the pronoun '你' refers to another group member who just spoke, choose 'other_user'."
                ),
                "criteria": {
                    "bot": "Directly addressing, questioning, or replying to the companion bot.",
                    "other_user": "Addressing another group member (e.g., '你' refers to a member who just spoke, or peer-to-peer banter).",
                    "third_person_about_bot": "Talking ABOUT the bot in the third person rather than speaking directly to it.",
                    "group_open": "Open remark, joke, or question addressed to the whole group.",
                },
            },
            "should_chime_in": {
                "type": "noul",
                "instructions": "Would a brief witty reply or banter from the companion bot feel natural and non-intrusive here?",
                "criteria": {
                    "true": "A short, natural reply or banter from the bot fits the moment well.",
                    "false": "The bot should stay quiet because members are talking to each other or chiming in would feel awkward.",
                },
            },
        }

        ordered = self._order_providers_with_cooldown(providers)
        for provider in ordered:
            api_key = decrypt_secret(provider.api_key_enc, self.master_key)
            t0 = time.perf_counter()
            try:
                if provider.protocol == "typesafe_systemone":
                    if self._systemone_caller is not None:
                        raw = await self._systemone_caller(provider, api_key, state_text, questions)
                    else:
                        raw = await self._call_typesafe_systemone(provider, api_key, state_text, questions, timeout_override=timeout_override)
                    parsed = self._parse_jev_response(raw, default_target)
                    usage = raw.get("usage") or {}
                    p_tok = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                else:
                    import json as _json
                    messages = [
                        {
                            "role": "system",
                            "content": '你是群聊归属门卫。务必结合最近几条群聊上下文判断最新消息里的“你”是在对刚发言的群友说话(other_user)还是对机器人说话(bot/group_open)。若明显是群友间互聊，必须判 target="other_user" 且 score<=0.2。仅输出 JSON：{"target":"bot|other_user|third_person_about_bot|group_open","score":0.0-1.0,"reason":"简短原因"}',
                        },
                        {"role": "user", "content": state_text},
                    ]
                    if self._http_caller is not None:
                        raw = await self._http_caller(provider, api_key, messages, 0.1, 100)
                    else:
                        raw = await self._call_openai_compatible(
                            provider, api_key, messages, 0.1, 160,
                            session_scope=session_scope, gatekeeper_json=True,
                            timeout_override=timeout_override, disable_thinking=True,
                        )
                    txt = raw.get("choices", [{}])[0].get("message", {}).get("content", "{}")
                    parsed = _json.loads(txt)
                    p_tok = int((raw.get("usage") or {}).get("prompt_tokens", 0))

                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                was_cooling = self.clear_cooldown(provider.id)
                if was_cooling:
                    logger.info("Gatekeeper provider '%s' recovered from cooldown!", provider.name)
                provider.last_health_status = "healthy"
                provider.last_health_ms = elapsed_ms
                provider.last_checked_at = datetime.now(timezone.utc)
                cost = (p_tok / 1_000_000.0) * provider.price_input_per_1m
                session.add(
                    UsageMetric(
                        slot="gatekeeper",
                        provider_name=provider.name,
                        model_name=provider.model_name,
                        prompt_tokens=p_tok,
                        estimated_cost_usd=cost,
                        ttft_ms=elapsed_ms,
                        total_ms=elapsed_ms,
                        source="production",
                        request_id=request_id,
                        success=True,
                    )
                )
                await session.flush()
                return parsed
            except Exception as exc:
                logger.warning("Gatekeeper provider '%s' (%s) failed [%s]: %s", provider.name, provider.protocol, type(exc).__name__, exc)
                await self._record_provider_failure(session, provider, exc, has_alternative=(len(providers) > 1))
                session.add(UsageMetric(slot="gatekeeper", provider_name=provider.name, model_name=provider.model_name, total_ms=int((time.perf_counter()-t0)*1000), source="production", request_id=request_id, success=False))
                await session.flush()
                continue
        return None

    @staticmethod
    def _parse_jev_response(raw: dict[str, Any], default_target: str) -> dict[str, Any]:
        """Extract calibrated target (`choice`) and chime-in probability (`noul`) from TypeSafe System One response.

        Official response shape:
        {
          "answers": {
            "target": {"type": "choice", "choice": "other_user", "confidence": 0.94, "probabilities": {...}},
            "should_chime_in": {"type": "noul", "noul": 0.87}
          }
        }
        Also retains backward compatibility with legacy mock/custom payloads (`value` / `probabilities`).
        """
        answers = raw.get("answers") or raw.get("results") or raw
        target_ans = answers.get("target")
        chime_ans = answers.get("should_chime_in")

        target = default_target
        target_conf: float | None = None
        if isinstance(target_ans, dict):
            raw_choice = target_ans.get("choice")
            if raw_choice is None:
                raw_choice = target_ans.get("value")
            if raw_choice is not None:
                candidate = str(raw_choice).strip()
                if candidate in VALID_GATEKEEPER_TARGETS:
                    target = candidate
            if "confidence" in target_ans and target_ans["confidence"] is not None:
                try:
                    target_conf = float(target_ans["confidence"])
                except Exception:
                    target_conf = None
        elif isinstance(target_ans, str) and target_ans.strip() in VALID_GATEKEEPER_TARGETS:
            target = target_ans.strip()

        score = 0.35
        if isinstance(chime_ans, dict):
            if "noul" in chime_ans and chime_ans["noul"] is not None:
                score = float(chime_ans["noul"])
            else:
                val = chime_ans.get("value")
                probs = chime_ans.get("probabilities") or {}
                if "true" in probs or True in probs:
                    score = float(probs.get("true", probs.get(True, 0.5)))
                elif "confidence" in chime_ans and chime_ans["confidence"] is not None:
                    conf = float(chime_ans["confidence"])
                    score = conf if val else (1.0 - conf)
                elif val is not None:
                    score = 0.85 if val else 0.2
        elif isinstance(chime_ans, (int, float)) and not isinstance(chime_ans, bool):
            score = float(chime_ans)
        elif isinstance(chime_ans, bool):
            score = 0.85 if chime_ans else 0.2

        clamped_score = round(max(0.0, min(1.0, score)), 3)
        conf_part = f", conf={target_conf:.2f}" if target_conf is not None else ""
        return {
            "target": target,
            "score": clamped_score,
            "reason": f"Jev System One (target={target}{conf_part}, noul={clamped_score:.2f})",
        }

    async def probe_provider(self, session: AsyncSession, provider_id: int) -> dict[str, Any]:
        """One-click live health check tagged with source='probe' (excluded from prod stats).
        A successful probe also clears any active cooldown on the provider.
        """
        provider = await session.get(ModelProvider, provider_id)
        if provider is None:
            raise ValueError("Provider not found")

        api_key = decrypt_secret(provider.api_key_enc, self.master_key)
        t0 = time.perf_counter()
        try:
            if provider.protocol == "typesafe_systemone":
                q = {"is_ping": {"type": "noul", "instructions": "Is this a ping health check?"}}
                if self._systemone_caller is not None:
                    res = await self._systemone_caller(provider, api_key, "ping", q)
                else:
                    res = await self._call_typesafe_systemone(provider, api_key, "ping", q)
                reply = str(res.get("answers", res))[:80]
            elif self._http_caller is not None:
                res = await self._http_caller(provider, api_key, [{"role": "user", "content": "ping"}], 0.0, 16)
                reply = res.get("choices", [{}])[0].get("message", {}).get("content", "ok")
            else:
                res = await self._call_openai_compatible(provider, api_key, [{"role": "user", "content": "Reply OK"}], 0.0, 16)
                reply = res.get("choices", [{}])[0].get("message", {}).get("content", "OK")

            ms = int((time.perf_counter() - t0) * 1000)
            self.clear_cooldown(provider.id)
            provider.last_health_status = "healthy"
            provider.last_health_ms = ms
            provider.last_checked_at = datetime.now(timezone.utc)
            session.add(
                UsageMetric(
                    slot=provider.slot,
                    provider_name=provider.name,
                    model_name=provider.model_name,
                    ttft_ms=ms,
                    total_ms=ms,
                    source="probe",
                    success=True,
                )
            )
            await session.flush()
            return {"ok": True, "latency_ms": ms, "sample_reply": str(reply)[:80]}
        except Exception as exc:
            ms = int((time.perf_counter() - t0) * 1000)
            provider.last_health_status = "error"
            provider.last_health_ms = ms
            provider.last_checked_at = datetime.now(timezone.utc)
            session.add(
                UsageMetric(
                    slot=provider.slot,
                    provider_name=provider.name,
                    model_name=provider.model_name,
                    total_ms=ms,
                    source="probe",
                    success=False,
                )
            )
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

    async def _call_openai_compatible(
        self,
        provider: ModelProvider,
        api_key: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        *,
        session_scope: str | None = None,
        gatekeeper_json: bool = False,
        timeout_override: float | None = None,
        disable_thinking: bool = False,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{provider.base_url.rstrip('/')}/chat/completions"
        extra = provider.extra_config or {}
        headers = {"Content-Type": "application/json"}
        if isinstance(extra.get("extra_headers"), dict):
            headers.update({str(k): str(v) for k, v in extra["extra_headers"].items()})
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload: dict[str, Any] = {
            "model": provider.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        if isinstance(extra.get("extra_body"), dict):
            payload.update(extra["extra_body"])
        if disable_thinking and "thinking" not in payload and "reasoning_effort" not in payload:
            if "deepseek" in provider.model_name.lower() or "qwen" in provider.model_name.lower():
                payload["thinking"] = {"type": "disabled"}

        endpoint = urlsplit(url)
        if endpoint.scheme == "https" and endpoint.hostname == "opencode.ai" and endpoint.path.startswith(
            ("/zen/go/", "/inference/go/")
        ):
            scope = session_scope or uuid.uuid4().hex
            digest = hmac.new(self.master_key.encode(), f"{provider.slot}:{scope}".encode(), hashlib.sha256).hexdigest()
            headers["x-opencode-session"] = f"ses_{digest[:32]}"
            headers["User-Agent"] = "CompanionSystem/1.3"
            if provider.slot == "gatekeeper" and provider.model_name == "deepseek-v4.1-flash":
                payload.setdefault("thinking", {"type": "disabled"})
                payload.setdefault("reasoning_effort", "none")
                if gatekeeper_json:
                    payload.setdefault("response_format", {"type": "json_object"})

        timeout_val = min(float(provider.timeout_seconds), float(timeout_override)) if timeout_override else float(provider.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout_val) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _build_systemone_url(base_url: str) -> str:
        base = base_url.rstrip("/")
        if base.endswith("/systemone"):
            return base
        if base.endswith("/v1"):
            return f"{base}/systemone"
        return f"{base}/v1/systemone"

    @classmethod
    async def _call_typesafe_systemone(
        cls,
        provider: ModelProvider,
        api_key: str,
        state_text: str,
        questions: dict[str, Any],
        timeout_override: float | None = None,
    ) -> dict[str, Any]:
        """Native TypeSafe AI System One endpoint: POST /v1/systemone (or OpenCode /zen/v1/systemone)."""
        url = cls._build_systemone_url(provider.base_url)
        extra = provider.extra_config or {}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if isinstance(extra.get("extra_headers"), dict):
            headers.update({str(k): str(v) for k, v in extra["extra_headers"].items()})
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload: dict[str, Any] = {
            "model": provider.model_name,
            "state": state_text,
            "questions": questions,
        }
        if isinstance(extra.get("extra_body"), dict):
            payload.update(extra["extra_body"])
        timeout_val = min(float(provider.timeout_seconds), float(timeout_override)) if timeout_override else float(provider.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout_val) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()
