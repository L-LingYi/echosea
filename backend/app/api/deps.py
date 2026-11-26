"""FastAPI dependencies for DB sessions, runtime services, and JWT authentication."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import AdminUser
from app.security import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    db_mgr = request.app.state.db_manager
    async with db_mgr.session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_current_admin(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_db),
) -> AdminUser:
    token = creds.credentials if creds else request.cookies.get("companion_session")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    settings = request.app.state.settings
    payload = decode_access_token(token, settings.jwt_secret_key)
    if not payload or not payload.get("sub"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    stmt = select(AdminUser).where(AdminUser.username == payload["sub"], AdminUser.is_active.is_(True))
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    token_ver = payload.get("tv")
    if token_ver is None or int(token_ver) != int(getattr(user, "token_version", 1)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session revoked; please log in again")
    return user


def get_runtime(request: Request) -> Any:
    return request.app.state
