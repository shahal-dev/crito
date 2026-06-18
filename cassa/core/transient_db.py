"""Transient follow-up data model.

Alerts (raw broker ingest), Candidates (a visibility-surviving alert for one
night), ObservationRequests (an approved candidate + imaging recipe), Execution
Blocks/Steps (the on-sky plan), and an append-only AuditEvent log.

These tables register on the **same** declarative ``Base`` as ``Image`` (imported
from ``db.py``), so ``DB.init()``'s ``create_all`` creates them automatically —
importing this module is enough to register them. ``app.py`` imports it in the
lifespan before ``db.init()``.

State columns are plain strings validated by the ``str``-enum constants below
(SQLite-friendly; a clean Alembic migration at the Postgres cutover).

NOTE: ``create_all`` creates *missing* tables but never ALTERs an existing one.
During development, a schema change to these tables means dropping
``data/cassa.db`` (same workflow as ``Image`` today).
"""
from __future__ import annotations

import datetime
import enum

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, _utcnow


# --------------------------------------------------------------------- enums
class CandidateState(str, enum.Enum):
    NEW = "new"                      # passed visibility, not yet shown
    NOTIFIED = "notified"            # posted to Slack/email, awaiting decision
    APPROVED_QUEUE = "approved_queue"      # approve -> attended queue
    APPROVED_EXECUTE = "approved_execute"  # approve -> execute now / auto
    REJECTED = "rejected"
    EXPIRED = "expired"              # window passed without a decision


class RequestState(str, enum.Enum):
    PENDING = "pending"
    QUEUED = "queued"
    READY = "ready"
    DONE = "done"
    CANCELLED = "cancelled"


class BlockState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"


class StepState(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunMode(str, enum.Enum):
    ATTENDED = "attended"   # operator launches from the queue
    AUTO = "auto"           # sequencer dispatches when observable (guarded)


class _DictMixin:
    """Serialize a row to a JSON-friendly dict (ISO-format datetimes), matching
    the ``Image.dict()`` convention used elsewhere in the archive."""

    def dict(self) -> dict:
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        for k, v in d.items():
            if isinstance(v, datetime.datetime):
                d[k] = v.isoformat()
        return d


# --------------------------------------------------------------------- models
class Alert(_DictMixin, Base):
    """A raw broker object, normalized. Kept for audit + reprocessing when the
    filters change. One row per broker object id (``oid``)."""

    __tablename__ = "alert"

    id: Mapped[str] = mapped_column(String, primary_key=True)   # broker oid
    source: Mapped[str] = mapped_column(String, default="alerce", index=True)
    received_utc: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen_utc: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    ra_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    dec_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    class_label: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    class_prob: Mapped[float | None] = mapped_column(Float, nullable=True)
    mag_last: Mapped[float | None] = mapped_column(Float, nullable=True)
    ndethist: Mapped[int | None] = mapped_column(Integer, nullable=True)
    firstmjd: Mapped[float | None] = mapped_column(Float, nullable=True)
    lastmjd: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Candidate(_DictMixin, Base):
    """A visibility-surviving alert for a single night (``id = oid_utdate``)."""

    __tablename__ = "candidate"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    alert_id: Mapped[str] = mapped_column(String, index=True)
    ut_date: Mapped[str] = mapped_column(String, index=True)
    class_label: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    class_prob: Mapped[float | None] = mapped_column(Float, nullable=True)
    ra_deg: Mapped[float] = mapped_column(Float)
    dec_deg: Mapped[float] = mapped_column(Float)
    mag: Mapped[float | None] = mapped_column(Float, nullable=True)
    state: Mapped[str] = mapped_column(String, default=CandidateState.NEW.value, index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    # tonight's observability (UTC ISO strings; null window = not observable)
    window_start_utc: Mapped[str | None] = mapped_column(String, nullable=True)
    window_end_utc: Mapped[str | None] = mapped_column(String, nullable=True)
    max_alt_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_airmass: Mapped[float | None] = mapped_column(Float, nullable=True)
    moon_sep_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    moon_illum_frac: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    notified_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)


class ObservationRequest(_DictMixin, Base):
    """An approved candidate + an imaging recipe to carry out."""

    __tablename__ = "observation_request"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String, index=True)
    object_name: Mapped[str] = mapped_column(String, default="")
    ra_deg: Mapped[float] = mapped_column(Float)
    dec_deg: Mapped[float] = mapped_column(Float)
    # recipe_json: [{"filter_slot": int|null, "exptime_s": float, "count": int, "dither_px": int}]
    recipe_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    mode: Mapped[str] = mapped_column(String, default=RunMode.ATTENDED.value)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String, default=RequestState.PENDING.value, index=True)
    window_start_utc: Mapped[str | None] = mapped_column(String, nullable=True)
    window_end_utc: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)


class ExecutionBlock(_DictMixin, Base):
    """One runnable on-sky unit (one request)."""

    __tablename__ = "execution_block"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    request_id: Mapped[str] = mapped_column(String, index=True)
    state: Mapped[str] = mapped_column(String, default=BlockState.QUEUED.value, index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    total_steps: Mapped[int] = mapped_column(Integer, default=0)
    n_done: Mapped[int] = mapped_column(Integer, default=0)
    n_failed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)


class ExecutionStep(_DictMixin, Base):
    """One step of a block: slew | center | autofocus | filter | expose."""

    __tablename__ = "execution_step"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    block_id: Mapped[str] = mapped_column(String, index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String)
    params_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String, default=StepState.PENDING.value)
    image_id: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)


class Plan(_DictMixin, Base):
    """A saved, named observation plan (a reusable template). Running one expands
    its recipe (repeat × exposure sets × count) into an ExecutionBlock + steps."""

    __tablename__ = "plan"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, index=True)          # the "plan file name"
    object_name: Mapped[str] = mapped_column(String, default="")
    ra_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    dec_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    # recipe_json: [{filter_slot, filter_name, exptime_s, count, binning, dither_px}]
    recipe_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    repeat: Mapped[int] = mapped_column(Integer, default=1)
    autofocus: Mapped[bool] = mapped_column(Boolean, default=False)
    center: Mapped[bool] = mapped_column(Boolean, default=False)   # plate-solve center (stub)
    source: Mapped[str | None] = mapped_column(String, nullable=True)  # "manual" | "candidate:<id>" | "queue:<block>"
    last_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_block_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_run_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow)


class AuditEvent(_DictMixin, Base):
    """Append-only log of every state transition — the audit source of truth."""

    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    actor: Mapped[str] = mapped_column(String)            # "slack:U…" | "email:…" | "console" | "system"
    action: Mapped[str] = mapped_column(String)
    entity_type: Mapped[str] = mapped_column(String, index=True)
    entity_id: Mapped[str] = mapped_column(String, index=True)
    detail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[str | None] = mapped_column(String, nullable=True)
