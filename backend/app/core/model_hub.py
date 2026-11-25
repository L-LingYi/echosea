"""Unified Model Control Center (v1.1):
- 6 standard slots, Primary+Fallback failover, AES-GCM encrypted keys.
- Native TypeSafe AI Jev System One (`typesafe_systemone` -> POST /v1/systemone) support for Gatekeeper!
- Separates UsageMetric `source` ('production' | 'probe' | 'reflection' | 'sandbox') so probes never pollute stats.
- Auto-updates provider `last_health_status = 'healthy'` on successful live calls.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

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
        systemone_caller: Callable[[ModelProvider, str, str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.master_key = master_key
        self.alert_service = alert_service
        self.daily_budget_usd = daily_budget_usd
        self.budget_alert_ratio = budget_alert_ratio
        self._http_caller = http_caller
        self._systemone_caller = systemone_caller
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
    ) -> ChatCompletionResult:
        """Invoke primary provider for slot; seamlessly failover to fallback provider on error."""
        stmt = (
            select(ModelProvider)
            .where(ModelProvider.slot == slot, ModelProvider.is_enabled.is_(True))
            .order_by(ModelProvider.role.desc())
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
                    raw = await self._call_openai_compatible(
                        provider, api_key, messages, temperature, max_tokens, session_scope=session_scope
                    )

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

                # Auto-refresh provider health on live success (fixes 'unknown' status in diagnostics!)
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
        or standard `openai_compatible` fallback. Returns {'target': ..., 'score': ..., 'reason': ...} or None.
        """
        stmt = (
            select(ModelProvider)
            .where(ModelProvider.slot == "gatekeeper", ModelProvider.is_enabled.is_(True))
            .order_by(ModelProvider.role.desc())
        )
        providers = list((await session.execute(stmt)).scalars().all())
        if not providers:
            return None

        questions = {
            "target": {
                "type": "enum",
                "options": ["bot", "other_user", "third_person_about_bot", "group_open"],
                "instructions": "Using the recent group chat context, classify who the latest message is addressed to. If the pronoun '你' refers to another group member who just spoke, choose 'other_user'.",
            },
            "should_chime_in": {
                "type": "boolean",
                "instructions": "Would a brief witty reply or banter from the companion bot feel natural and non-intrusive here?",
            },
        }

        for provider in providers:
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
                    # OpenAI-compatible JSON gatekeeper fallback
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
                session.add(UsageMetric(slot="gatekeeper", provider_name=provider.name, model_name=provider.model_name, total_ms=int((time.perf_counter()-t0)*1000), source="production", request_id=request_id, success=False))
                await session.flush()
                continue
        return None

    @staticmethod
    def _parse_jev_response(raw: dict[str, Any], default_target: str) -> dict[str, Any]:
        """Extract calibrated target and chime-in probability from TypeSafe System One response."""
        answers = raw.get("answers") or raw.get("results") or raw
        target_ans = answers.get("target")
        chime_ans = answers.get("should_chime_in")

        target = default_target
        if isinstance(target_ans, dict):
            target = str(target_ans.get("value") or default_target)
        elif isinstance(target_ans, str):
            target = target_ans

        score = 0.35
        if isinstance(chime_ans, dict):
            val = chime_ans.get("value")
            probs = chime_ans.get("probabilities") or {}
            if "true" in probs or True in probs:
                score = float(probs.get("true", probs.get(True, 0.5)))
            elif "confidence" in chime_ans:
                conf = float(chime_ans["confidence"])
                score = conf if val else (1.0 - conf)
            else:
                score = 0.85 if val else 0.2
        elif isinstance(chime_ans, bool):
            score = 0.85 if chime_ans else 0.2

        return {
            "target": target,
            "score": round(max(0.0, min(1.0, score)), 3),
            "reason": f"Jev System One (target={target}, p={score:.2f})",
        }

    async def probe_provider(self, session: AsyncSession, provider_id: int) -> dict[str, Any]:
        """One-click live health check tagged with source='probe' (excluded from prod stats)."""
        provider = await session.get(ModelProvider, provider_id)
        if provider is None:
            raise ValueError("Provider not found")

        api_key = decrypt_secret(provider.api_key_enc, self.master_key)
        t0 = time.perf_counter()
        try:
            if provider.protocol == "typesafe_systemone":
                q = {"is_ping": {"type": "boolean", "instructions": "Is this a ping health check?"}}
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
    async def _call_typesafe_systemone(
        provider: ModelProvider,
        api_key: str,
        state_text: str,
        questions: dict[str, Any],
        timeout_override: float | None = None,
    ) -> dict[str, Any]:
        """Native TypeSafe AI System One endpoint: POST /v1/systemone."""
        base = provider.base_url.rstrip("/")
        url = base if base.endswith("/systemone") else f"{base}/v1/systemone"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": provider.model_name,
            "state": state_text,
            "questions": questions,
        }
        timeout_val = min(float(provider.timeout_seconds), float(timeout_override)) if timeout_override else float(provider.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout_val) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()
