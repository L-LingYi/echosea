"""Cloud Observability Dashboard API (v1.1):
- Excludes health probes (`source='probe'`) from production TTFT / P95 / cost stats.
- Reports separate TTS synthesis vs OneBot acceptance counts.
- Exposes privacy-safe unified `request_id` Pipeline Traces (`/api/dashboard/traces`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    TTSDelivery,
    UsageMetric,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/overview")
async def dashboard_overview(
    days: int = Query(default=7, ge=1, le=90),
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Strictly filter production traffic (exclude source='probe' so probes never skew TTFT/P95)
    u_stmt = select(
        func.coalesce(func.sum(UsageMetric.prompt_tokens), 0),
        func.coalesce(func.sum(UsageMetric.cached_tokens), 0),
        func.coalesce(func.sum(UsageMetric.completion_tokens), 0),
        func.coalesce(func.sum(UsageMetric.estimated_cost_usd), 0.0),
        func.coalesce(func.avg(UsageMetric.ttft_ms), 0.0),
        func.count(UsageMetric.id),
    ).where(UsageMetric.created_at >= since, UsageMetric.source != "probe")
    prompt_tok, cached_tok, comp_tok, total_cost, avg_ttft, total_calls = (await session.execute(u_stmt)).one()

    # Compute P95 TTFT on production samples
    lat_stmt = (
        select(UsageMetric.ttft_ms)
        .where(UsageMetric.created_at >= since, UsageMetric.source != "probe", UsageMetric.success.is_(True))
        .order_by(UsageMetric.ttft_ms.asc())
    )
    latencies = [int(x) for x in (await session.execute(lat_stmt)).scalars().all()]
    p95_ttft = latencies[int(len(latencies) * 0.95)] if latencies else 0

    cache_hit_rate = round((cached_tok / prompt_tok) * 100, 2) if prompt_tok else 0.0

    slot_stmt = (
        select(
            UsageMetric.slot,
            func.coalesce(func.sum(UsageMetric.prompt_tokens + UsageMetric.completion_tokens), 0),
            func.coalesce(func.sum(UsageMetric.estimated_cost_usd), 0.0),
            func.count(UsageMetric.id),
        )
        .where(UsageMetric.created_at >= since, UsageMetric.source != "probe")
        .group_by(UsageMetric.slot)
    )
    by_slot = [
        {"slot": r[0], "tokens": int(r[1]), "cost_usd": round(float(r[2]), 6), "calls": int(r[3])}
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
    tts_success_rate = round((tts_onebot_accepted / tts_total) * 100, 2) if tts_total else 100.0

    identities_count = int((await session.execute(select(func.count(QQIdentity.qq_id)))).scalar_one() or 0)
    outbox_sent = int(
        (await session.execute(select(func.count(OutboxDelivery.id)).where(OutboxDelivery.status == "sent"))).scalar_one()
        or 0
    )
    stickers_approved = int(
        (await session.execute(select(func.count(StickerAsset.id)).where(StickerAsset.status == "approved"))).scalar_one()
        or 0
    )
    recent_alerts = list(
        (await session.execute(select(AlertEvent).order_by(AlertEvent.created_at.desc()).limit(5))).scalars().all()
    )

    today_spend = await runtime.model_hub.get_today_spend_usd(session)
    budget_limit = runtime.model_hub.daily_budget_usd

    return {
        "window_days": days,
        "tokens": {
            "prompt_tokens": int(prompt_tok),
            "cached_tokens": int(cached_tok),
            "completion_tokens": int(comp_tok),
            "prompt_cache_hit_rate_pct": cache_hit_rate,
            "total_calls": int(total_calls),
            "avg_ttft_ms": round(float(avg_ttft), 1),
            "p95_ttft_ms": p95_ttft,
        },
        "cost": {
            "window_cost_usd": round(float(total_cost), 6),
            "today_spend_usd": round(today_spend, 6),
            "daily_budget_usd": budget_limit,
            "budget_fuse_active": today_spend >= budget_limit if budget_limit > 0 else False,
        },
        "tts": {
            "total_attempts": tts_total,
            "synthesized_count": tts_synthesized,
            "onebot_accepted_count": tts_onebot_accepted,
            "sent_count": tts_onebot_accepted,
            "success_rate_pct": tts_success_rate,
        },
        "by_slot": by_slot,
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
