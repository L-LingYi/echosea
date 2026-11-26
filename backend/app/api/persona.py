"""Persona Versioning, 3-Layer Config Inheritance, and Group Chat Replay Sandbox API."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, get_runtime
from app.core.gatekeeper import GatekeeperService
from app.core.redis_store import StateStore
from app.models.db import EvolutionSuggestion, PersonaVersion, SystemConfig
from app.models.schemas import ConfigUpsert, MessageEnvelope, PersonaCreate, SandboxReplayRequest, SuggestionReviewRequest

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


@router.post("/sandbox/benchmark")
async def sandbox_benchmark(
    payload: dict,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    """Diagnostic Rec #3: Run labeled group-chat benchmark to measure Gatekeeper FP/FN & voice-intent accuracy."""
    from app.core.tts_service import detect_voice_intent

    persona = await runtime.pipeline.get_active_persona(session)
    aliases = payload.get("override_aliases") or list(persona.aliases or [])
    threshold = float(payload.get("override_soft_entry_threshold", runtime.gatekeeper.soft_entry_threshold))
    samples = payload.get("samples") or []

    sim_store = StateStore(force_memory=True)
    sim_gk = GatekeeperService(store=sim_store, soft_entry_threshold=threshold)

    false_positives = 0  # Expected silent, predicted reply (误接)
    false_negatives = 0  # Expected reply, predicted silent (漏接)
    voice_mismatches = 0
    details = []

    for idx, s in enumerate(samples):
        env = MessageEnvelope(
            message_id=f"bench_{idx}",
            scene="group",
            group_id=int(s.get("group_id", 999999)),
            sender_qq=int(s.get("sender_qq", 10000 + idx)),
            sender_nickname=str(s.get("sender_name", f"样本{idx}")),
            raw_text=str(s.get("text", "")),
            mentioned_qqs=[int(x) for x in s.get("mentioned_qqs", [])],
            expanded_reply_text=s.get("expanded_reply_text"),
            self_qq=int(s.get("self_qq", runtime.settings.bot_qq or 888888)),
        )
        decision = await sim_gk.evaluate(
            envelope=env,
            bot_name=persona.bot_name,
            aliases=aliases,
            replied_to_bot=bool(s.get("replied_to_bot", False)),
        )
        predicted_reply = decision.action in ("direct_pass", "soft_entry")
        expected_reply = bool(s.get("expected_reply", False))
        if predicted_reply and not expected_reply:
            false_positives += 1
        elif not predicted_reply and expected_reply:
            false_negatives += 1

        voice_pred = detect_voice_intent(env.raw_text, has_reply_quote=bool(env.expanded_reply_text))
        voice_exp = s.get("expected_voice_intent")
        if voice_exp and voice_pred != voice_exp:
            voice_mismatches += 1

        details.append(
            {
                "text": env.raw_text,
                "expected_reply": expected_reply,
                "predicted_action": decision.action,
                "score": decision.score,
                "voice_intent": voice_pred,
                "ok": (predicted_reply == expected_reply) and (not voice_exp or voice_pred == voice_exp),
            }
        )

    total = len(samples)
    correct = sum(1 for d in details if d["ok"])
    return {
        "total_samples": total,
        "accuracy_pct": round((correct / total) * 100, 2) if total else 100.0,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "voice_mismatches": voice_mismatches,
        "details": details,
    }


@router.get("/suggestions")
async def list_evolution_suggestions(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    """List pending Few-Shot candidates, banned phrases, core persona advices, and active 48h correction hints."""
    stmt = select(EvolutionSuggestion).order_by(EvolutionSuggestion.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(EvolutionSuggestion.status == status)
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        {
            "id": r.id,
            "suggestion_type": r.suggestion_type,
            "status": r.status,
            "group_id": r.group_id,
            "source_request_id": r.source_request_id,
            "title": r.title,
            "content_json": r.content_json,
            "reason": r.reason,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            "created_at": r.created_at.isoformat(),
            "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        }
        for r in rows
    ]


@router.post("/suggestions/{suggestion_id}/review")
async def review_evolution_suggestion(
    suggestion_id: int,
    payload: SuggestionReviewRequest,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    """Approve or reject a controlled evolution suggestion (never auto-rewrites system_prompt)."""
    sug = await session.get(EvolutionSuggestion, suggestion_id)
    if sug is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    now_utc = datetime.now(timezone.utc)
    sug.reviewed_at = now_utc

    if payload.action == "reject":
        sug.status = "expired" if sug.suggestion_type == "correction_hint" else "rejected"
        await session.flush()
        return {"ok": True, "id": sug.id, "status": sug.status}

    persona = await runtime.pipeline.get_active_persona(session)
    content = sug.content_json or {}
    if sug.suggestion_type == "few_shot":
        user_txt = str(content.get("user") or "").strip()
        asst_txt = str(content.get("assistant") or "").strip()
        if user_txt and asst_txt:
            examples = list(persona.few_shot_examples or [])
            if not any(ex.get("user") == user_txt and ex.get("assistant") == asst_txt for ex in examples):
                examples.append({"user": user_txt, "assistant": asst_txt})
                persona.few_shot_examples = examples[-12:]
        sug.status = "approved"
    elif sug.suggestion_type == "banned_phrase":
        phrase = str(content.get("phrase") or "").strip()
        if phrase:
            banned = list(persona.banned_phrases or [])
            if phrase not in banned:
                banned.append(phrase)
                persona.banned_phrases = banned
        sug.status = "approved"
    elif sug.suggestion_type == "persona_advice":
        # Marked approved for admin tracking; core system_prompt is only updated via explicit PersonaVersion creation
        sug.status = "approved"
    else:
        sug.status = "active"

    await session.flush()
    return {"ok": True, "id": sug.id, "status": sug.status, "suggestion_type": sug.suggestion_type}
