"""Persona Versioning, 3-Layer Config Inheritance, and Group Chat Replay Sandbox API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, get_runtime
from app.core.gatekeeper import GatekeeperService
from app.core.redis_store import StateStore
from app.models.db import PersonaVersion, SystemConfig
from app.models.schemas import ConfigUpsert, MessageEnvelope, PersonaCreate, SandboxReplayRequest

router = APIRouter(prefix="/api/persona", tags=["persona_and_config"])


@router.get("/versions")
async def list_personas(
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    await runtime.pipeline.get_active_persona(session)
    stmt = select(PersonaVersion).order_by(PersonaVersion.id.desc())
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        {
            "id": r.id,
            "version_tag": r.version_tag,
            "bot_name": r.bot_name,
            "aliases": r.aliases,
            "system_prompt": r.system_prompt,
            "few_shot_examples": r.few_shot_examples,
            "banned_phrases": r.banned_phrases,
            "is_active": r.is_active,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.post("/versions")
async def create_persona(
    payload: PersonaCreate,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    if payload.activate:
        await session.execute(update(PersonaVersion).values(is_active=False))
    persona = PersonaVersion(
        version_tag=payload.version_tag,
        bot_name=payload.bot_name,
        aliases=payload.aliases,
        system_prompt=payload.system_prompt,
        few_shot_examples=payload.few_shot_examples,
        banned_phrases=payload.banned_phrases,
        is_active=payload.activate,
    )
    session.add(persona)
    await session.flush()
    return {"ok": True, "id": persona.id, "version_tag": persona.version_tag}


@router.post("/versions/{persona_id}/activate")
async def activate_persona(
    persona_id: int,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    target = await session.get(PersonaVersion, persona_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Persona version not found")
    await session.execute(update(PersonaVersion).values(is_active=False))
    target.is_active = True
    return {"ok": True, "active_version": target.version_tag}


@router.get("/configs")
async def list_configs(
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    rows = list((await session.execute(select(SystemConfig).order_by(SystemConfig.scope_type.asc()))).scalars().all())
    return [
        {
            "id": r.id,
            "scope_type": r.scope_type,
            "scope_id": r.scope_id,
            "config_json": r.config_json,
        }
        for r in rows
    ]


@router.put("/configs")
async def upsert_config(
    payload: ConfigUpsert,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    stmt = select(SystemConfig).where(
        SystemConfig.scope_type == payload.scope_type,
        SystemConfig.scope_id == payload.scope_id,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is None:
        row = SystemConfig(
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            config_json=payload.config_json,
        )
        session.add(row)
    else:
        existing.config_json = payload.config_json
    await session.flush()
    return {"ok": True}


@router.post("/sandbox/replay")
async def sandbox_replay(
    payload: SandboxReplayRequest,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    """Blind Spot #5: Offline Group Chat Replay Sandbox to tune Gatekeeper thresholds safely."""
    persona = await runtime.pipeline.get_active_persona(session)
    aliases = payload.override_aliases if payload.override_aliases is not None else list(persona.aliases or [])
    threshold = (
        payload.override_soft_entry_threshold
        if payload.override_soft_entry_threshold is not None
        else runtime.gatekeeper.soft_entry_threshold
    )
    sim_store = StateStore(force_memory=True)
    sim_gatekeeper = GatekeeperService(store=sim_store, soft_entry_threshold=threshold)

    results = []
    for idx, msg in enumerate(payload.messages):
        env = MessageEnvelope(
            message_id=f"sim_{idx}",
            scene="group",
            group_id=payload.group_id,
            sender_qq=int(msg.get("sender_qq", 10000 + idx)),
            sender_nickname=str(msg.get("sender_name", f"群友{idx}")),
            raw_text=str(msg.get("text", "")),
            mentioned_qqs=[int(x) for x in msg.get("mentioned_qqs", [])],
            self_qq=int(msg.get("self_qq", runtime.settings.bot_qq or 888888)),
        )
        decision = await sim_gatekeeper.evaluate(
            envelope=env,
            bot_name=persona.bot_name,
            aliases=aliases,
            replied_to_bot=bool(msg.get("replied_to_bot", False)),
        )
        results.append(
            {
                "sender": env.display_name,
                "text": env.raw_text,
                "decision": decision.model_dump(),
            }
        )
    return {"bot_name": persona.bot_name, "aliases": aliases, "threshold": threshold, "results": results}
