"""Model Control Center API (v1.1.1): Unified CRUD + PATCH partial update preserving extra_config."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, get_runtime
from app.models.db import ModelProvider
from app.models.schemas import ModelProviderPatch, ModelProviderUpsert
from app.security import encrypt_secret

router = APIRouter(prefix="/api/providers", tags=["model_control_center"])


@router.get("")
async def list_providers(
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    stmt = select(ModelProvider).order_by(ModelProvider.slot.asc(), ModelProvider.role.desc())
    providers = list((await session.execute(stmt)).scalars().all())
    return {
        "slots": list(runtime.model_hub.VALID_SLOTS),
        "items": [runtime.model_hub.to_safe_view(p) for p in providers],
    }


@router.put("")
async def upsert_provider(
    payload: ModelProviderUpsert,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    provider = await runtime.model_hub.upsert_provider(session, payload.model_dump())
    return runtime.model_hub.to_safe_view(provider)


@router.patch("/{provider_id}")
async def patch_provider(
    provider_id: int,
    payload: ModelProviderPatch,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    """Partial update by provider ID; never overwrites api_key or extra_config unless explicitly provided."""
    provider = await session.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    updates = payload.model_dump(exclude_unset=True)
    for field in ("name", "protocol", "model_name", "rate_limit_rpm", "timeout_seconds", "price_input_per_1m", "price_output_per_1m", "is_enabled"):
        if field in updates and updates[field] is not None:
            setattr(provider, field, updates[field])
    if updates.get("base_url"):
        provider.base_url = str(updates["base_url"]).rstrip("/")
    if updates.get("api_key"):
        provider.api_key_enc = encrypt_secret(str(updates["api_key"]), runtime.settings.master_encryption_key)
    if "extra_config" in updates and updates["extra_config"] is not None:
        provider.extra_config = updates["extra_config"]
    await session.flush()
    return runtime.model_hub.to_safe_view(provider)


@router.post("/{provider_id}/test")
async def test_provider(
    provider_id: int,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    try:
        return await runtime.model_hub.probe_provider(session, provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/{provider_id}/reset-cooldown")
async def reset_provider_cooldown(
    provider_id: int,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    """Manually clear an active failover cooldown so primary traffic resumes immediately."""
    provider = await session.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    cleared = runtime.model_hub.clear_cooldown(provider.id)
    if provider.last_health_status == "cooldown":
        provider.last_health_status = "unknown"
        await session.flush()
    return {"ok": True, "cleared": cleared, "provider": runtime.model_hub.to_safe_view(provider)}


@router.delete("/{provider_id}")
async def delete_provider(
    provider_id: int,
    _admin=Depends(get_current_admin),
    session: AsyncSession = Depends(get_db),
):
    provider = await session.get(ModelProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    await session.delete(provider)
    return {"ok": True}
