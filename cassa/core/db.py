"""Database: async SQLAlchemy models + engine.

Phase 1 defaults to SQLite (zero-infra dev). The same models run on PostgreSQL +
TimescaleDB later by changing CASSA_DB_URL — see docs/plan/07-DATABASE.md. (Alembic
migrations replace create_all at the Postgres cutover.)
"""
from __future__ import annotations

import datetime
import pathlib
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Base(DeclarativeBase):
    pass


class Image(Base):
    __tablename__ = "image"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    obsid: Mapped[str] = mapped_column(String, index=True)
    ut_date: Mapped[str] = mapped_column(String, index=True)
    date_obs: Mapped[str] = mapped_column(String)
    exptime: Mapped[float] = mapped_column(Float)
    image_type: Mapped[str] = mapped_column(String, index=True)
    object_name: Mapped[str] = mapped_column(String, default="")
    filter: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ra_deg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dec_deg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    alt_deg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    az_deg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    airmass: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    focus_pos: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    telescope: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    instrument: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    observer: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sha256: Mapped[str] = mapped_column(String)
    fits_key: Mapped[str] = mapped_column(String)
    preview_key: Mapped[str] = mapped_column(String, default="")
    thumb_key: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    def dict(self) -> dict:
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        ts = d.get("created_at")
        if isinstance(ts, datetime.datetime):
            d["created_at"] = ts.isoformat()
        return d


class User(Base):
    __tablename__ = "user"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, default="viewer")  # viewer|observer|operator|admin
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)

    def dict(self) -> dict:
        return {"id": self.id, "username": self.username, "role": self.role,
                "created_at": self.created_at.isoformat() if self.created_at else None}


class DB:
    def __init__(self, url: str):
        # ensure a parent dir exists for file-based sqlite urls
        if url.startswith("sqlite") and ":///" in url:
            fpath = url.split(":///", 1)[1]
            if fpath and fpath != ":memory:":
                pathlib.Path(fpath).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(url, future=True)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
