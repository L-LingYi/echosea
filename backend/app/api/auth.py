"""Cloud Admin Authentication API: Login with IP rate limit, JWT/HttpOnly cookie, TOTP 2FA."""
from __future__ import annotations

import ipaddress
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import bearer_scheme, get_current_admin, get_db, get_runtime
from app.models.db import AdminUser
from app.models.schemas import LoginRequest, PasswordChangeRequest, TokenResponse
from app.security import (
    create_access_token,
    decode_access_token,
    decrypt_secret,
    encrypt_secret,
    generate_totp_secret,
    get_totp_provisioning_uri,
    hash_password,
    verify_password,
    verify_totp_code,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


_TRUSTED_PROXY_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)


def _is_trusted_proxy(host: str | None) -> bool:
    if not host:
        return False
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    try:
        ip_obj = ipaddress.ip_address(host.split("%")[0])
        return ip_obj.is_loopback or any(ip_obj in net for net in _TRUSTED_PROXY_NETS)
    except ValueError:
        return False


def _extract_client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if _is_trusted_proxy(peer):
        real_ip = (request.headers.get("x-real-ip") or "").strip()
        if real_ip:
            return real_ip
        forwarded = (request.headers.get("x-forwarded-for") or "").strip()
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer


def _is_https_request(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else ""
    if _is_trusted_proxy(peer):
        proto = (request.headers.get("x-forwarded-proto") or "").strip().lower()
        return proto == "https"
    return False


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    client_ip = _extract_client_ip(request)
    store = getattr(runtime, "state_store", None)
    limiter = runtime.login_rate_limiter
    if store is not None:
        if await store.is_login_blocked(client_ip):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed login attempts. Please retry in 5 minutes.",
            )
    elif limiter.is_blocked(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Please retry in 5 minutes.",
        )

    stmt = select(AdminUser).where(AdminUser.username == payload.username, AdminUser.is_active.is_(True))
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        if store is not None:
            await store.record_login_failure(client_ip)
        else:
            limiter.record_failure(client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    if user.totp_enabled and user.totp_secret_enc:
        secret = decrypt_secret(user.totp_secret_enc, runtime.settings.master_encryption_key)
        if not verify_totp_code(secret, payload.totp_code or ""):
            if store is not None:
                await store.record_login_failure(client_ip)
            else:
                limiter.record_failure(client_ip)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid 2FA TOTP code")

    if store is not None:
        await store.reset_login_failures(client_ip)
    limiter.reset(client_ip)
    user.last_login_at = datetime.now(timezone.utc)
    token = create_access_token(
        subject=user.username,
        secret_key=runtime.settings.jwt_secret_key,
        expires_minutes=runtime.settings.jwt_expire_minutes,
        extra={"tv": int(getattr(user, "token_version", 1))},
    )
    response.set_cookie(
        key="companion_session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=_is_https_request(request),
        max_age=runtime.settings.jwt_expire_minutes * 60,
    )
    return TokenResponse(access_token=token, username=user.username, totp_enabled=user.totp_enabled)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_db),
    runtime=Depends(get_runtime),
):
    token = creds.credentials if creds else request.cookies.get("companion_session")
    if token:
        payload = decode_access_token(token, runtime.settings.jwt_secret_key)
        if payload and payload.get("sub"):
            stmt = select(AdminUser).where(AdminUser.username == payload["sub"], AdminUser.is_active.is_(True))
            user = (await session.execute(stmt)).scalar_one_or_none()
            if user is not None:
                user.token_version = int(getattr(user, "token_version", 1)) + 1
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
    response: Response,
    admin: AdminUser = Depends(get_current_admin),
):
    if not verify_password(payload.old_password, admin.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Old password is incorrect")
    admin.password_hash = hash_password(payload.new_password)
    admin.token_version = int(getattr(admin, "token_version", 1)) + 1
    response.delete_cookie("companion_session")
    return {"ok": True}


@router.post("/totp/setup")
async def setup_totp(
    admin: AdminUser = Depends(get_current_admin),
    runtime=Depends(get_runtime),
):
    """Generate a pending TOTP secret without modifying or disabling any currently active 2FA."""
    import time as _time
    secret = generate_totp_secret()
    pending = getattr(runtime, "pending_totp_secrets", None)
    if pending is None:
        pending = {}
        runtime.pending_totp_secrets = pending
    pending[admin.id] = {
        "secret_enc": encrypt_secret(secret, runtime.settings.master_encryption_key),
        "expires_at": _time.time() + 600.0,
    }
    uri = get_totp_provisioning_uri(secret, admin.username)
    return {"secret": secret, "provisioning_uri": uri, "pending": True, "totp_enabled": admin.totp_enabled}


@router.post("/totp/enable")
async def enable_totp(
    body: dict,
    admin: AdminUser = Depends(get_current_admin),
    runtime=Depends(get_runtime),
):
    """Verify 6-digit code against pending TOTP secret, then atomically activate it."""
    import time as _time
    pending_map = getattr(runtime, "pending_totp_secrets", None) or {}
    pending = pending_map.get(admin.id)
    candidate_enc: str | None = None
    if pending and float(pending.get("expires_at", 0.0)) >= _time.time():
        candidate_enc = str(pending["secret_enc"])
    elif admin.totp_secret_enc and not admin.totp_enabled:
        candidate_enc = admin.totp_secret_enc

    if not candidate_enc:
        raise HTTPException(status_code=400, detail="Run /totp/setup first")
    secret = decrypt_secret(candidate_enc, runtime.settings.master_encryption_key)
    if not verify_totp_code(secret, str(body.get("code", ""))):
        raise HTTPException(status_code=400, detail="Invalid TOTP verification code")
    admin.totp_secret_enc = candidate_enc
    admin.totp_enabled = True
    pending_map.pop(admin.id, None)
    return {"ok": True, "totp_enabled": True}
