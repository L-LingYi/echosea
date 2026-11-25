"""Memory, QQ Identity Cards, Slang Review, and One-Click Forget API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, get_runtime
from app.models.db import AmbientSummary, GroupExpression, MemoryEpisode, QQIdentity, SelfLifeState
from app.models.schemas import ExpressionReviewRequest, ForgetRequest, IdentityUpdate

router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.get("/identities")
async def list_identities(
    limit: int = Query(default=100, ge=1, le=500),
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    items = await runtime.identity_service.list_identities(session, limit=limit)
    return [
        {
            "qq_id": i.qq_id,
            "nickname": i.nickname,
            "group_cards": i.group_cards,
            "known_names": i.known_names,
            "relationship_level": i.relationship_level,
            "public_profile": i.public_profile,
            "private_profile": i.private_profile,
            "last_seen_at": i.last_seen_at.isoformat(),
        }
        for i in items
    ]


@router.patch("/identities/{qq_id}")
async def update_identity(
    qq_id: int,
    payload: IdentityUpdate,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    identity = await session.get(QQIdentity, qq_id)
    if identity is None:
        raise HTTPException(status_code=404, detail="QQ identity not found")
    if payload.relationship_level is not None:
        identity.relationship_level = payload.relationship_level
    if payload.public_profile is not None:
        identity.public_profile = payload.public_profile
    if payload.private_profile is not None:
        identity.private_profile = payload.private_profile
    return {"ok": True, "qq_id": qq_id}


@router.get("/ambient")
async def list_ambient_summaries(
    group_id: int | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    stmt = select(AmbientSummary).order_by(AmbientSummary.window_end.desc()).limit(limit)
    if group_id is not None:
        stmt = stmt.where(AmbientSummary.group_id == group_id)
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        {
            "id": r.id,
            "group_id": r.group_id,
            "window_start": r.window_start.isoformat(),
            "window_end": r.window_end.isoformat(),
            "summary_text": r.summary_text,
            "message_count": r.message_count,
            "active_participants": r.active_participants,
        }
        for r in rows
    ]


@router.get("/episodes")
async def list_episodes(
    scope: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    stmt = select(MemoryEpisode).order_by(MemoryEpisode.created_at.desc()).limit(limit)
    if scope:
        stmt = stmt.where(MemoryEpisode.scope == scope)
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        {
            "id": r.id,
            "scope": r.scope,
            "group_id": r.group_id,
            "qq_id": r.qq_id,
            "title": r.title,
            "content": r.content,
            "importance": r.importance,
            "is_anniversary": r.is_anniversary,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/expressions")
async def list_expressions(
    status: str | None = None,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    stmt = select(GroupExpression).order_by(GroupExpression.created_at.desc()).limit(100)
    if status:
        stmt = stmt.where(GroupExpression.status == status)
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        {
            "id": r.id,
            "group_id": r.group_id,
            "phrase": r.phrase,
            "meaning": r.meaning,
            "status": r.status,
            "usage_count": r.usage_count,
        }
        for r in rows
    ]


@router.patch("/expressions/{expr_id}")
async def review_expression(
    expr_id: int,
    payload: ExpressionReviewRequest,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    expr = await session.get(GroupExpression, expr_id)
    if expr is None:
        raise HTTPException(status_code=404, detail="Expression not found")
    expr.status = payload.status
    if payload.meaning is not None:
        expr.meaning = payload.meaning
    return {"ok": True, "id": expr_id, "status": expr.status}


@router.get("/self-state")
async def get_self_state(
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    stmt = select(SelfLifeState).order_by(SelfLifeState.recorded_at.desc()).limit(1)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return {"energy_level": 80, "mood": "relaxed", "current_activity": "摸鱼看群", "diary_entry": ""}
    return {
        "energy_level": row.energy_level,
        "mood": row.mood,
        "current_activity": row.current_activity,
        "diary_entry": row.diary_entry,
        "recorded_at": row.recorded_at.isoformat(),
    }


@router.post("/forget")
async def one_click_forget(
    payload: ForgetRequest,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    """Blind Spot #4: One-Click Forget cascading across messages, episodes, summaries, and profiles."""
    stats = await runtime.memory_service.forget_user_data(
        session=session,
        qq_id=payload.qq_id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        wipe_profile=payload.wipe_profile,
    )
    return {"ok": True, "qq_id": payload.qq_id, **stats}
