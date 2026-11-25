"""Cloud Observability Dashboard API (v1.1):
- Excludes health probes (`source='probe'`) from production TTFT / P95 / cost stats.
- Reports separate TTS synthesis vs OneBot acceptance counts.
- Exposes privacy-safe unified `request_id` Pipeline Traces (`/api/dashboard/traces`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, get_runtime
from app.models.db import (
    AlertEvent,
    ChatMessage,
    OutboxDelivery,
    PipelineTrace,
    QQIdentity,
    StickerAsset,
    SystemConfig,
    TTSDelivery,
    UsageMetric,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _reviewed_ids(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [item for item in value if type(item) is int and item > 0]


def _to_local(dt: datetime, tz_delta: timedelta) -> datetime:
    utc_dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return utc_dt + tz_delta


def _local_today_start_utc(now_utc: datetime, tz_offset_minutes: int) -> datetime:
    """Return the UTC timestamp corresponding to 00:00:00 of the caller's local day."""
    tz_delta = timedelta(minutes=tz_offset_minutes)
    today_local = (now_utc + tz_delta).date()
    return datetime(today_local.year, today_local.month, today_local.day, tzinfo=timezone.utc) - tz_delta


async def _build_usage_series(
    session: AsyncSession,
    now_utc: datetime,
    days: int,
    tz_offset_minutes: int,
) -> dict:
    tz_delta = timedelta(minutes=tz_offset_minutes)
    now_local = now_utc + tz_delta
    today_local = now_local.date()
    start_day_local = today_local - timedelta(days=days - 1)
    start_day_utc = _local_today_start_utc(now_utc, tz_offset_minutes) - timedelta(days=days - 1)

    current_hour_local = now_local.replace(minute=0, second=0, microsecond=0)
    start_hour_local = current_hour_local - timedelta(hours=23)
    start_hour_utc = start_hour_local.replace(tzinfo=timezone.utc) - tz_delta

    series_since = min(start_day_utc, start_hour_utc)

    daily_map: dict[str, dict] = {}
    for offset in range(days):
        d = start_day_local + timedelta(days=offset)
        key = d.isoformat()
        daily_map[key] = {
            "date": key,
            "label": d.strftime("%m-%d"),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "tokens": 0,
            "tts_chars": 0,
            "calls": 0,
        }

    hourly_map: dict[str, dict] = {}
    for offset in range(24):
        h = start_hour_local + timedelta(hours=offset)
        key = h.strftime("%Y-%m-%d %H:00")
        hourly_map[key] = {
            "hour": key,
            "label": h.strftime("%H:00"),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "tokens": 0,
            "tts_chars": 0,
            "calls": 0,
        }

    usage_rows = (
        await session.execute(
            select(
                UsageMetric.created_at,
                UsageMetric.prompt_tokens,
                UsageMetric.completion_tokens,
            ).where(UsageMetric.created_at >= series_since, UsageMetric.source != "probe")
        )
    ).all()

    for created_at, p_tok, c_tok in usage_rows:
        if created_at is None:
            continue
        local_dt = _to_local(created_at, tz_delta)
        p_val, c_val = int(p_tok or 0), int(c_tok or 0)
        d_key = local_dt.date().isoformat()
        if d_key in daily_map:
            daily_map[d_key]["prompt_tokens"] += p_val
            daily_map[d_key]["completion_tokens"] += c_val
            daily_map[d_key]["tokens"] += p_val + c_val
            daily_map[d_key]["calls"] += 1
        h_key = local_dt.strftime("%Y-%m-%d %H:00")
        if h_key in hourly_map:
            hourly_map[h_key]["prompt_tokens"] += p_val
            hourly_map[h_key]["completion_tokens"] += c_val
            hourly_map[h_key]["tokens"] += p_val + c_val
            hourly_map[h_key]["calls"] += 1

    tts_rows = (
        await session.execute(
            select(
                TTSDelivery.created_at,
                func.length(TTSDelivery.cleaned_text),
            ).where(TTSDelivery.created_at >= series_since, TTSDelivery.synthesis_status == "synthesized")
        )
    ).all()

    for created_at, text_len in tts_rows:
        if created_at is None:
            continue
        local_dt = _to_local(created_at, tz_delta)
        chars_val = int(text_len or 0)
        d_key = local_dt.date().isoformat()
        if d_key in daily_map:
            daily_map[d_key]["tts_chars"] += chars_val
        h_key = local_dt.strftime("%Y-%m-%d %H:00")
        if h_key in hourly_map:
            hourly_map[h_key]["tts_chars"] += chars_val

    return {
        "daily": list(daily_map.values()),
        "hourly": list(hourly_map.values()),
    }


@router.get("/overview")
async def dashboard_overview(
    days: int = Query(default=7, ge=1, le=90),
    tz_offset_minutes: int = Query(default=480, ge=-720, le=840),
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    now_utc = datetime.now(timezone.utc)
    since = now_utc - timedelta(days=days)
    review = (
        await session.execute(
            select(SystemConfig.config_json).where(
                SystemConfig.scope_type == "global", SystemConfig.scope_id == "dashboard_latency"
            )
        )
    ).scalar_one_or_none()
    review = review if isinstance(review, dict) else {}
    excluded_usage_ids = _reviewed_ids(review.get("excluded_usage_ids"))
    excluded_alert_ids = _reviewed_ids(review.get("excluded_alert_ids"))
    latency_is_included = UsageMetric.id.not_in(excluded_usage_ids)

    # Strictly filter production traffic (exclude source='probe' so probes never skew TTFT/P95)
    u_stmt = select(
        func.coalesce(func.sum(UsageMetric.prompt_tokens), 0),
        func.coalesce(func.sum(UsageMetric.cached_tokens), 0),
        func.coalesce(func.sum(UsageMetric.completion_tokens), 0),
        func.coalesce(func.sum(UsageMetric.estimated_cost_usd), 0.0),
        func.coalesce(func.avg(UsageMetric.ttft_ms).filter(latency_is_included), 0.0),
        func.count(UsageMetric.id),
        func.count(UsageMetric.id).filter(~latency_is_included),
    ).where(UsageMetric.created_at >= since, UsageMetric.source != "probe")
    prompt_tok, cached_tok, comp_tok, total_cost, avg_ttft, total_calls, excluded_calls = (
        await session.execute(u_stmt)
    ).one()

    # Compute P95 TTFT on production samples
    lat_stmt = (
        select(UsageMetric.ttft_ms)
        .where(
            UsageMetric.created_at >= since,
            UsageMetric.source != "probe",
            UsageMetric.success.is_(True),
            latency_is_included,
        )
        .order_by(UsageMetric.ttft_ms.asc())
    )
    latencies = [int(x) for x in (await session.execute(lat_stmt)).scalars().all()]
    p95_ttft = latencies[int(len(latencies) * 0.95)] if latencies else 0

    cache_hit_rate = round((cached_tok / prompt_tok) * 100, 2) if prompt_tok else 0.0

    start_of_today = _local_today_start_utc(now_utc, tz_offset_minutes)
    today_tok_stmt = select(
        func.coalesce(func.sum(UsageMetric.prompt_tokens), 0),
        func.coalesce(func.sum(UsageMetric.completion_tokens), 0),
        func.coalesce(func.sum(UsageMetric.cached_tokens), 0),
    ).where(UsageMetric.created_at >= start_of_today, UsageMetric.source != "probe")
    today_prompt_tok, today_comp_tok, today_cached_tok = (await session.execute(today_tok_stmt)).one()

    slot_stmt = (
        select(
            UsageMetric.slot,
            func.coalesce(func.sum(UsageMetric.prompt_tokens), 0),
            func.coalesce(func.sum(UsageMetric.completion_tokens), 0),
            func.coalesce(func.sum(UsageMetric.cached_tokens), 0),
            func.count(UsageMetric.id),
        )
        .where(UsageMetric.created_at >= since, UsageMetric.source != "probe")
        .group_by(UsageMetric.slot)
    )
    by_slot = [
        {
            "slot": r[0],
            "prompt_tokens": int(r[1]),
            "completion_tokens": int(r[2]),
            "cached_tokens": int(r[3]),
            "tokens": int(r[1]) + int(r[2]),
            "calls": int(r[4]),
        }
        for r in (await session.execute(slot_stmt)).all()
    ]

    gk_stmt = (
        select(ChatMessage.gatekeeper_decision, func.count(ChatMessage.id))
        .where(ChatMessage.created_at >= since)
        .group_by(ChatMessage.gatekeeper_decision)
    )
    gk_counts = {r[0]: int(r[1]) for r in (await session.execute(gk_stmt)).all()}
    total_msgs = sum(gk_counts.values())
    passed_msgs = gk_counts.get("direct_pass", 0) + gk_counts.get("soft_entry", 0)
    gatekeeper_pass_rate = round((passed_msgs / total_msgs) * 100, 2) if total_msgs else 0.0

    # Separated TTS synthesis_status vs onebot_status metrics
    tts_total = int(
        (await session.execute(select(func.count(TTSDelivery.id)).where(TTSDelivery.created_at >= since))).scalar_one() or 0
    )
    tts_synthesized = int(
        (
            await session.execute(
                select(func.count(TTSDelivery.id)).where(
                    TTSDelivery.created_at >= since, TTSDelivery.synthesis_status == "synthesized"
                )
            )
        ).scalar_one()
        or 0
    )
    tts_onebot_submitted = int(
        (
            await session.execute(
                select(func.count(TTSDelivery.id)).where(
                    TTSDelivery.created_at >= since, TTSDelivery.onebot_status.in_(("submitted", "accepted"))
                )
            )
        ).scalar_one()
        or 0
    )
    tts_onebot_accepted = int(
        (
            await session.execute(
                select(func.count(TTSDelivery.id)).where(
                    TTSDelivery.created_at >= since, TTSDelivery.onebot_status == "accepted"
                )
            )
        ).scalar_one()
        or 0
    )
    # TTS usage measured in synthesized Unicode character count (no DB migration needed)
    tts_chars_window = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(func.length(TTSDelivery.cleaned_text)), 0)).where(
                    TTSDelivery.created_at >= since, TTSDelivery.synthesis_status == "synthesized"
                )
            )
        ).scalar_one()
        or 0
    )
    tts_chars_today = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(func.length(TTSDelivery.cleaned_text)), 0)).where(
                    TTSDelivery.created_at >= start_of_today, TTSDelivery.synthesis_status == "synthesized"
                )
            )
        ).scalar_one()
        or 0
    )
    if tts_synthesized > 0 or tts_total > 0:
        by_slot.append(
            {
                "slot": "tts",
                "unit": "chars",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cached_tokens": 0,
                "tokens": 0,
                "chars": tts_chars_window,
                "calls": tts_synthesized,
            }
        )

    # Preserve original synthesis success rate metric & expose independent OneBot acceptance rate
    tts_success_rate = round((tts_synthesized / tts_total) * 100, 2) if tts_total else 100.0
    tts_accept_rate = round((tts_onebot_accepted / tts_total) * 100, 2) if tts_total else None

    identities_count = int((await session.execute(select(func.count(QQIdentity.qq_id)))).scalar_one() or 0)
    outbox_sent = int(
        (await session.execute(select(func.count(OutboxDelivery.id)).where(OutboxDelivery.status == "sent"))).scalar_one()
        or 0
    )
    stickers_approved = int(
        (await session.execute(select(func.count(StickerAsset.id)).where(StickerAsset.status == "approved"))).scalar_one()
        or 0
    )
    slow_response_alerts = int(
        (
            await session.execute(
                select(func.count(AlertEvent.id)).where(
                    AlertEvent.created_at >= since,
                    AlertEvent.category == "slow_response",
                    AlertEvent.id.not_in(excluded_alert_ids),
                )
            )
        ).scalar_one()
        or 0
    )
    recent_alerts = list(
        (await session.execute(select(AlertEvent).order_by(AlertEvent.created_at.desc()).limit(5))).scalars().all()
    )

    get_today_spend = runtime.model_hub.get_today_spend_usd
    if "start_of_day" in inspect.signature(get_today_spend).parameters:
        today_spend = await get_today_spend(session, start_of_day=start_of_today)
    else:
        today_spend = await get_today_spend(session)
    budget_limit = runtime.model_hub.daily_budget_usd
    usage_series = await _build_usage_series(session, now_utc, max(days, 30), tz_offset_minutes)

    return {
        "window_days": days,
        "tokens": {
            "today_prompt_tokens": int(today_prompt_tok),
            "today_completion_tokens": int(today_comp_tok),
            "today_cached_tokens": int(today_cached_tok),
            "today_total_tokens": int(today_prompt_tok) + int(today_comp_tok),
            "prompt_tokens": int(prompt_tok),
            "cached_tokens": int(cached_tok),
            "completion_tokens": int(comp_tok),
            "total_tokens": int(prompt_tok) + int(comp_tok),
            "prompt_cache_hit_rate_pct": cache_hit_rate,
            "total_calls": int(total_calls),
            "avg_ttft_ms": round(float(avg_ttft), 1),
            "p95_ttft_ms": p95_ttft,
            "slow_response_alerts": slow_response_alerts,
            "latency_excluded_calls": int(excluded_calls),
        },
        "cost": {
            "window_cost_usd": round(float(total_cost), 6),
            "today_spend_usd": round(today_spend, 6),
            "daily_budget_usd": budget_limit,
            "budget_fuse_active": today_spend >= budget_limit if budget_limit > 0 else False,
        },
        "tts": {
            "today_chars": tts_chars_today,
            "total_chars": tts_chars_window,
            "synthesized_chars": tts_chars_window,
            "total_attempts": tts_total,
            "synthesized_count": tts_synthesized,
            "onebot_submitted_count": tts_onebot_submitted,
            "onebot_accepted_count": tts_onebot_accepted,
            "sent_count": tts_synthesized,
            "success_rate_pct": tts_success_rate,
            "onebot_accept_rate_pct": tts_accept_rate,
            "onebot_acceptance_rate_pct": tts_accept_rate,
        },
        "by_slot": by_slot,
        "usage_series": usage_series,
        "gatekeeper": {
            "total_messages": total_msgs,
            "pass_rate_pct": gatekeeper_pass_rate,
            "breakdown": gk_counts,
        },
        "counts": {
            "qq_identities": identities_count,
            "outbox_sent": outbox_sent,
            "stickers_approved": stickers_approved,
        },
        "health": {
            "gateway": runtime.adapter.status_snapshot(),
            "state_store": runtime.store.backend_name,
            "plugins": runtime.plugin_manager.list_loaded(),
        },
        "recent_alerts": [
            {
                "id": a.id,
                "severity": a.severity,
                "category": a.category,
                "title": a.title,
                "created_at": a.created_at.isoformat(),
            }
            for a in recent_alerts
        ],
    }


@router.get("/traces")
async def list_pipeline_traces(
    limit: int = Query(default=60, ge=10, le=300),
    request_id: str | None = None,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    """Privacy-safe per-stage execution traces grouped by unified request_id (zero raw chat text)."""
    stmt = select(PipelineTrace).order_by(PipelineTrace.created_at.desc()).limit(limit)
    if request_id:
        stmt = stmt.where(PipelineTrace.request_id == request_id)
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        {
            "id": r.id,
            "request_id": r.request_id,
            "scene": r.scene,
            "group_id": r.group_id,
            "stage": r.stage,
            "status": r.status,
            "latency_ms": r.latency_ms,
            "error_type": r.error_type,
            "meta": r.meta_json,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
