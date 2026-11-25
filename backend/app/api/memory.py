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


@router.get("/graph")
async def consciousness_sea_graph(
    limit_identities: int = Query(default=40, ge=5, le=120),
    limit_episodes: int = Query(default=40, ge=5, le=120),
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    """Consciousness Sea (意识海): Multi-layer Memory Knowledge Graph for interactive WebUI visualization.

    Returns nodes & edges connecting:
    - L5 Self Core (EchoSea 自我意识)
    - Group Worlds (群聊域)
    - L4 QQ Identities (熟人卡 + 亲密度 + 公开/私密画像)
    - L2 Ambient Summaries (边缘旁听摘要)
    - L3 Episodes & Anniversaries (情景记忆，标记 public_group / private_dm 半隔离边界)
    - L4 Group Expressions (群黑话)
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    node_ids: set[str] = set()

    def _add_node(nid: str, label: str, layer: str, **extra) -> None:
        if nid not in node_ids:
            node_ids.add(nid)
            nodes.append({"id": nid, "label": label, "layer": layer, **extra})

    def _add_edge(src: str, dst: str, relation: str, strength: float = 0.6) -> None:
        if src in node_ids and dst in node_ids and src != dst:
            edges.append({"source": src, "target": dst, "relation": relation, "strength": strength})

    # 1. L5 Self Core Node
    life = (await session.execute(select(SelfLifeState).order_by(SelfLifeState.recorded_at.desc()).limit(1))).scalar_one_or_none()
    _add_node(
        "self_core",
        "EchoSea · 意识核心",
        "L5_self",
        size=28,
        summary=f"精力 {life.energy_level if life else 80} · 心情 {life.mood if life else 'relaxed'} · {life.current_activity if life else '摸鱼看群'}",
        detail={
            "energy_level": life.energy_level if life else 80,
            "mood": life.mood if life else "relaxed",
            "current_activity": life.current_activity if life else "摸鱼看群",
            "diary_entry": life.diary_entry if life else "",
        },
    )

    # 2. L4 QQ Identities & Discovered Group Worlds
    identities = list(
        (await session.execute(select(QQIdentity).order_by(QQIdentity.last_seen_at.desc()).limit(limit_identities)))
        .scalars()
        .all()
    )
    groups_seen: set[int] = set()
    for ident in identities:
        uid = f"qq_{ident.qq_id}"
        display = ident.nickname or str(ident.qq_id)
        has_private = bool(ident.private_profile)
        _add_node(
            uid,
            display,
            "L4_identity",
            size=min(22, 13 + max(0, ident.relationship_level) // 10),
            summary=f"QQ {ident.qq_id} · 亲密度 {ident.relationship_level}",
            detail={
                "qq_id": ident.qq_id,
                "nickname": ident.nickname,
                "known_names": ident.known_names,
                "relationship_level": ident.relationship_level,
                "public_profile": ident.public_profile,
                "private_profile": ident.private_profile,
                "has_private_isolation": has_private,
            },
        )
        _add_edge("self_core", uid, "熟人羁绊" if ident.relationship_level > 0 else "初识", strength=0.75)

        for gid_str, card in (ident.group_cards or {}).items():
            if gid_str.isdigit():
                gid = int(gid_str)
                groups_seen.add(gid)
                gnid = f"group_{gid}"
                _add_node(gnid, f"群 {gid}", "group", size=20, summary=f"群聊记忆域 {gid}", detail={"group_id": gid})
                _add_edge("self_core", gnid, "常驻群域", strength=0.85)
                _add_edge(uid, gnid, f"群名片:{card}" if card else "群成员", strength=0.6)

    # 3. L2 Ambient Rolling Summaries
    ambients = list(
        (await session.execute(select(AmbientSummary).order_by(AmbientSummary.window_end.desc()).limit(20))).scalars().all()
    )
    for amb in ambients:
        gnid = f"group_{amb.group_id}"
        _add_node(gnid, f"群 {amb.group_id}", "group", size=20, summary=f"群聊记忆域 {amb.group_id}", detail={"group_id": amb.group_id})
        _add_edge("self_core", gnid, "常驻群域", strength=0.85)
        aid = f"amb_{amb.id}"
        _add_node(
            aid,
            f"边缘旁听 #{amb.id}",
            "L2_ambient",
            size=12,
            summary=amb.summary_text[:72],
            detail={
                "group_id": amb.group_id,
                "message_count": amb.message_count,
                "summary_text": amb.summary_text,
                "window_end": amb.window_end.isoformat(),
            },
        )
        _add_edge(gnid, aid, "旁听沉淀", strength=0.55)
        for pqq in (amb.active_participants or [])[:6]:
            _add_edge(f"qq_{pqq}", aid, "参与讨论", strength=0.45)

    # 4. L3 Episodic Memories & Anniversaries (shows public_group vs private_dm semi-isolation!)
    episodes = list(
        (await session.execute(select(MemoryEpisode).order_by(MemoryEpisode.importance.desc(), MemoryEpisode.created_at.desc()).limit(limit_episodes)))
        .scalars()
        .all()
    )
    for ep in episodes:
        eid = f"ep_{ep.id}"
        layer = "L3_private_episode" if ep.scope == "private_dm" else "L3_public_episode"
        _add_node(
            eid,
            ep.title or f"情景 #{ep.id}",
            layer,
            size=16 if ep.is_anniversary else 13,
            summary=ep.content[:80],
            detail={
                "scope": ep.scope,
                "title": ep.title,
                "content": ep.content,
                "importance": ep.importance,
                "is_anniversary": ep.is_anniversary,
                "created_at": ep.created_at.isoformat(),
            },
        )
        if ep.qq_id:
            _add_edge(f"qq_{ep.qq_id}", eid, "私密记忆(半隔离)" if ep.scope == "private_dm" else "共同经历", strength=0.8)
        if ep.group_id:
            gnid = f"group_{ep.group_id}"
            _add_node(gnid, f"群 {ep.group_id}", "group", size=20, summary=f"群聊记忆域 {ep.group_id}", detail={"group_id": ep.group_id})
            _add_edge(gnid, eid, "群内情景", strength=0.65)
        if not ep.qq_id and not ep.group_id:
            _add_edge("self_core", eid, "自我回忆", strength=0.6)

    # 5. L4 Approved & Pending Group Slang
    slangs = list((await session.execute(select(GroupExpression).order_by(GroupExpression.usage_count.desc()).limit(30))).scalars().all())
    for sl in slangs:
        sid = f"slang_{sl.id}"
        _add_node(
            sid,
            f"梗·{sl.phrase}",
            "L4_slang",
            size=11,
            summary=f"{sl.meaning} ({sl.status})",
            detail={"group_id": sl.group_id, "phrase": sl.phrase, "meaning": sl.meaning, "status": sl.status, "usage_count": sl.usage_count},
        )
        gnid = f"group_{sl.group_id}"
        _add_node(gnid, f"群 {sl.group_id}", "group", size=20, summary=f"群聊记忆域 {sl.group_id}", detail={"group_id": sl.group_id})
        _add_edge("self_core", gnid, "常驻群域", strength=0.85)
        _add_edge(gnid, sid, "群黑话", strength=0.5)

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "identities": len(identities),
            "episodes": len(episodes),
            "ambient_summaries": len(ambients),
            "slangs": len(slangs),
        },
    }
