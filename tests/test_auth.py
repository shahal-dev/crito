"""Tests for auth + RBAC primitives (offline)."""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cassa.core.auth import (  # noqa: E402
    AuthService,
    decode_token,
    hash_password,
    make_token,
    role_rank,
    verify_password,
)


def test_password_hash_roundtrip():
    h = hash_password("s3cret")
    assert verify_password("s3cret", h)
    assert not verify_password("wrong", h)
    assert not verify_password("s3cret", "garbage")


def test_token_roundtrip_and_tamper():
    t = make_token("alice", "operator", "KEY")
    p = decode_token(t, "KEY")
    assert p["sub"] == "alice" and p["role"] == "operator"
    with pytest.raises(ValueError):
        decode_token(t, "wrong-secret")          # bad signature
    with pytest.raises(ValueError):
        decode_token(t[:-3] + "xxx", "KEY")        # tampered signature
    with pytest.raises(ValueError):
        decode_token(make_token("a", "viewer", "KEY", ttl=-10), "KEY")  # expired


def test_role_hierarchy():
    assert role_rank("admin") > role_rank("operator") > role_rank("observer") > role_rank("viewer")
    assert role_rank("viewer") > role_rank(None) == 0


async def _flow(tmp_path):
    from cassa.core.db import DB

    db = DB(f"sqlite+aiosqlite:///{tmp_path}/u.db")
    await db.init()
    auth = AuthService(SimpleNamespace(admin_user="admin", admin_password="pw"), db.sessionmaker)
    await auth.seed_admin()
    await auth.seed_admin()  # idempotent
    me = await auth.authenticate("admin", "pw")
    bad = await auth.authenticate("admin", "nope")
    op = await auth.create_user("op", "oppw", "operator")
    users = await auth.list_users()
    await db.dispose()
    return me, bad, op, users


def test_seed_and_user_crud(tmp_path):
    me, bad, op, users = asyncio.run(_flow(tmp_path))
    assert me and me["role"] == "admin"
    assert bad is None
    assert op["role"] == "operator"
    assert len(users) == 2 and any(u["username"] == "op" for u in users)
