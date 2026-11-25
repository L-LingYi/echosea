"""Model Control Center API: Unified CRUD for 6 capability slots, encrypted keys, one-click health check."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, get_runtime
from app.models.db import ModelProvider
from app.models.schemas import ModelProviderUpsert

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
