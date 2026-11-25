"""Cloud Admin Authentication API: Login with IP rate limit, JWT/HttpOnly cookie, TOTP 2FA."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_db, get_runtime
from app.models.db import AdminUser
from app.models.schemas import LoginRequest, PasswordChangeRequest, TokenResponse
from app.security import (
    create_access_token,
    decrypt_secret,
    encrypt_secret,
    generate_totp_secret,
    get_totp_provisioning_uri,
    hash_password,
    verify_password,
    verify_totp_code,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    client_ip = request.client.host if request.client else "unknown"
    limiter = runtime.login_rate_limiter
    if limiter.is_blocked(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Please retry in 5 minutes.",
        )

    stmt = select(AdminUser).where(AdminUser.username == payload.username, AdminUser.is_active.is_(True))
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        limiter.record_failure(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    if user.totp_enabled and user.totp_secret_enc:
        secret = decrypt_secret(user.totp_secret_enc, runtime.settings.master_encryption_key)
        if not verify_totp_code(secret, payload.totp_code or ""):
            limiter.record_failure(client_ip)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid 2FA TOTP code")

    limiter.reset(client_ip)
    user.last_login_at = datetime.now(timezone.utc)
    token = create_access_token(
        subject=user.username,
        secret_key=runtime.settings.jwt_secret_key,
        expires_minutes=runtime.settings.jwt_expire_minutes,
    )
    response.set_cookie(
        key="companion_session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=runtime.settings.jwt_expire_minutes * 60,
    )
    return TokenResponse(access_token=token, username=user.username, totp_enabled=user.totp_enabled)


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("companion_session")
    return {"ok": True}


@router.get("/me")
async def me(admin: AdminUser = Depends(get_current_admin)):
    return {
        "username": admin.username,
        "totp_enabled": admin.totp_enabled,
        "last_login_at": admin.last_login_at,
    }


@router.post("/password")
async def change_password(
    payload: PasswordChangeRequest,
    admin: AdminUser = Depends(get_current_admin),
):
    if not verify_password(payload.old_password, admin.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Old password is incorrect")
    admin.password_hash = hash_password(payload.new_password)
    return {"ok": True}


@router.post("/totp/setup")
async def setup_totp(
    admin: AdminUser = Depends(get_current_admin),
    runtime=Depends(get_runtime),
):
    secret = generate_totp_secret()
    admin.totp_secret_enc = encrypt_secret(secret, runtime.settings.master_encryption_key)
    admin.totp_enabled = False
    uri = get_totp_provisioning_uri(secret, admin.username)
    return {"secret": secret, "provisioning_uri": uri}


@router.post("/totp/enable")
async def enable_totp(
    body: dict,
    admin: AdminUser = Depends(get_current_admin),
    runtime=Depends(get_runtime),
):
    if not admin.totp_secret_enc:
        raise HTTPException(status_code=400, detail="Run /totp/setup first")
    secret = decrypt_secret(admin.totp_secret_enc, runtime.settings.master_encryption_key)
    if not verify_totp_code(secret, str(body.get("code", ""))):
        raise HTTPException(status_code=400, detail="Invalid TOTP verification code")
    admin.totp_enabled = True
    return {"ok": True, "totp_enabled": True}
