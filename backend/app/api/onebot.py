"""SnowLuma OneBot v11 Ingress Endpoints (Reverse WebSocket + HTTP Event Webhook)."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, get_runtime

logger = logging.getLogger(__name__)
router = APIRouter(tags=["snowluma_onebot"])


def _verify_onebot_token(expected: str, auth_header: str | None, query_token: str | None) -> bool:
    if not expected:
        return True
    if query_token and query_token == expected:
        return True
    if auth_header:
        token = auth_header.replace("Bearer ", "").replace("Token ", "").strip()
        return token == expected
    return False


@router.websocket("/ws/onebot/v11")
async def snowluma_reverse_ws(
    websocket: WebSocket,
    access_token: str | None = Query(default=None),
):
    """Reverse WebSocket endpoint for SnowLuma gateway to connect into Companion System."""
    runtime = websocket.app.state
    expected_token = runtime.settings.snowluma_access_token
    auth_header = websocket.headers.get("authorization")
    if not _verify_onebot_token(expected_token, auth_header, access_token):
        await websocket.close(code=4003, reason="Invalid SnowLuma access token")
        return

    await websocket.accept()
    runtime.adapter.attach_reverse_ws(websocket)
    logger.info("SnowLuma Reverse WebSocket connected")
    try:
        while True:
            raw_text = await websocket.receive_text()
            data = json.loads(raw_text)
            if runtime.adapter.resolve_echo(data):
                continue
            if data.get("post_type") == "meta_event":
                runtime.adapter.record_heartbeat()
                continue
            if runtime.shutting_down:
                continue
            async def _handle_bg(ev: dict) -> None:
                async with runtime.db_manager.session_factory() as session:
                    try:
                        await runtime.pipeline.handle_raw_onebot_event(session, ev)
                        await session.commit()
                    except Exception as exc:
                        await session.rollback()
                        logger.exception("Error processing SnowLuma WS event: %s", exc)

            asyncio.create_task(_handle_bg(data))
    except WebSocketDisconnect:
        logger.warning("SnowLuma Reverse WebSocket disconnected")
    finally:
        runtime.adapter.detach_reverse_ws()


@router.post("/api/onebot/event")
async def snowluma_http_event(
    payload: dict,
    authorization: str | None = Header(default=None),
    access_token: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    """Optional HTTP Post webhook ingress for SnowLuma OneBot v11 events."""
    if not _verify_onebot_token(runtime.settings.snowluma_access_token, authorization, access_token):
        raise HTTPException(status_code=403, detail="Invalid SnowLuma access token")
    if runtime.shutting_down:
        return {"status": "draining"}
    return await runtime.pipeline.handle_raw_onebot_event(session, payload)


@router.get("/api/onebot/status")
async def gateway_status(
    _admin=Depends(get_current_admin),
    runtime=Depends(get_runtime),
):
    return runtime.adapter.status_snapshot()
