"""Authentication + role-based access control.

Self-contained: PBKDF2 password hashing and HS256 JWTs, both stdlib (no extra
deps). Tokens are signed with ``CRITO_AUTH_SECRET`` — set the SAME secret on every
site backend and one login works across all of them (the token is self-contained
with the username + role, so a site backend only needs the secret to validate it).

Role hierarchy (higher includes lower):
    viewer (read) < observer (plan/curate) < operator (control hardware) < admin (manage users)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid

from sqlalchemy import select

from .db import User, _utcnow

log = logging.getLogger("crito.auth")

ROLES = {"viewer": 1, "observer": 2, "operator": 3, "admin": 4}


def role_rank(role: str | None) -> int:
    return ROLES.get(role or "", 0)


# ------------------------------------------------------------- passwords
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), 200_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


# --------------------------------------------------------------- HS256 JWT
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(sub: str, role: str, secret: str, ttl: int = 7 * 86400) -> str:
    header = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64u(json.dumps({"sub": sub, "role": role, "exp": int(time.time()) + ttl},
                               separators=(",", ":")).encode())
    seg = f"{header}.{payload}"
    sig = _b64u(hmac.new(secret.encode(), seg.encode(), hashlib.sha256).digest())
    return f"{seg}.{sig}"


def decode_token(token: str, secret: str) -> dict:
    """Return the validated payload, or raise ValueError."""
    try:
        header, payload, sig = token.split(".")
    except (ValueError, AttributeError):
        raise ValueError("malformed token")
    expected = _b64u(hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise ValueError("bad signature")
    data = json.loads(_b64u_dec(payload))
    if int(data.get("exp", 0)) < int(time.time()):
        raise ValueError("token expired")
    return data


# --------------------------------------------------------------- service
class AuthService:
    def __init__(self, settings, sessionmaker):
        self.s = settings
        self.sm = sessionmaker

    async def seed_admin(self) -> None:
        """Create the default admin on first run (only if there are no users)."""
        async with self.sm() as session:
            existing = (await session.execute(select(User).limit(1))).first()
            if existing:
                return
            session.add(User(id=uuid.uuid4().hex, username=self.s.admin_user,
                             password_hash=hash_password(self.s.admin_password), role="admin"))
            await session.commit()
            log.warning("seeded default admin '%s' — log in and change the password!",
                        self.s.admin_user)

    async def authenticate(self, username: str, password: str) -> dict | None:
        async with self.sm() as session:
            u = (await session.execute(
                select(User).where(User.username == username))).scalar_one_or_none()
            if u and verify_password(password, u.password_hash):
                return u.dict()
            return None

    async def list_users(self) -> list[dict]:
        async with self.sm() as session:
            rows = (await session.execute(select(User).order_by(User.username))).scalars().all()
            return [u.dict() for u in rows]

    async def create_user(self, username: str, password: str, role: str) -> dict:
        username = (username or "").strip()
        if not username or not password:
            raise ValueError("username and password required")
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        async with self.sm() as session:
            dup = (await session.execute(
                select(User).where(User.username == username))).scalar_one_or_none()
            if dup:
                raise ValueError("username already exists")
            u = User(id=uuid.uuid4().hex, username=username,
                     password_hash=hash_password(password), role=role)
            session.add(u)
            await session.commit()
            return u.dict()

    async def delete_user(self, uid: str) -> None:
        async with self.sm() as session:
            u = await session.get(User, uid)
            if u:
                await session.delete(u)
                await session.commit()

    async def set_password(self, uid: str, password: str) -> bool:
        async with self.sm() as session:
            u = await session.get(User, uid)
            if not u:
                return False
            u.password_hash = hash_password(password)
            await session.commit()
            return True
