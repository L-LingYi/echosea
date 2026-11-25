"""Security utilities: AES-256-GCM secret encryption, PBKDF2 password hashing, JWT, TOTP, IP rate limiting."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import pyotp
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _derive_aes_key(master_key: str) -> bytes:
    return hashlib.sha256(master_key.encode("utf-8")).digest()


def encrypt_secret(plaintext: str, master_key: str) -> str:
    """Encrypt sensitive credentials (API keys, SMTP passwords) using AES-256-GCM."""
    if not plaintext:
        return ""
    key = _derive_aes_key(master_key)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(token: str, master_key: str) -> str:
    """Decrypt AES-256-GCM encrypted credentials."""
    if not token:
        return ""
    raw = base64.urlsafe_b64decode(token.encode("ascii"))
    nonce, ciphertext = raw[:12], raw[12:]
    key = _derive_aes_key(master_key)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")


def mask_secret(plaintext: str) -> str:
    """Return masked secret for WebUI display (never expose raw keys to frontend)."""
    if not plaintext:
        return ""
    if len(plaintext) <= 8:
        return "*" * len(plaintext)
    return f"{plaintext[:4]}••••{plaintext[-4:]}"


def hash_password(password: str) -> str:
    """Hash password with PBKDF2-HMAC-SHA256 and random 16-byte salt."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"pbkdf2_sha256$200000${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time password verification."""
    try:
        scheme, iter_s, salt_b64, dk_b64 = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iter_s))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def create_access_token(
    subject: str,
    secret_key: str,
    expires_minutes: int = 720,
    extra: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, secret_key, algorithm="HS256")


def decode_access_token(token: str, secret_key: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, secret_key, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp_code(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    totp = pyotp.TOTP(secret)
    return totp.verify(code.strip(), valid_window=1)


def get_totp_provisioning_uri(secret: str, username: str, issuer: str = "CompanionSystem") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


class LoginRateLimiter:
    """In-memory sliding-window IP rate limiter for login brute-force protection."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = defaultdict(list)

    def _prune(self, ip: str, now: float) -> None:
        cutoff = now - self.window_seconds
        self._attempts[ip] = [t for t in self._attempts[ip] if t > cutoff]

    def is_blocked(self, ip: str) -> bool:
        now = time.monotonic()
        self._prune(ip, now)
        return len(self._attempts[ip]) >= self.max_attempts

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        self._prune(ip, now)
        self._attempts[ip].append(now)

    def reset(self, ip: str) -> None:
        self._attempts.pop(ip, None)
