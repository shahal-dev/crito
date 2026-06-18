"""PlanService — saved, named observation plans and running/resuming them.

A Plan is a reusable template: a target (manual RA/Dec or carried over from a queue
entry) + a recipe of exposure sets (filter, exposure, count, binning, dither),
optionally repeated. Running a plan expands it into an ExecutionBlock + ordered
steps that the existing sequencer runs:

    slew → [center] → [autofocus] → repeat × (∀set: ∀count: expose [+ dither])

Resume re-launches the plan's last incomplete block; the sequencer skips steps that
already completed, so an interrupted plan continues where it stopped.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from ..core.db import _utcnow
from ..core.transient_db import (
    BlockState,
    ExecutionBlock,
    ExecutionStep,
    ObservationRequest,
    Plan,
    RequestState,
    RunMode,
    StepState,
)

log = logging.getLogger("cassa.transient.plans")

_INCOMPLETE = (BlockState.QUEUED.value, BlockState.RUNNING.value, BlockState.PAUSED.value,
               BlockState.FAILED.value, BlockState.ABORTED.value)


def _uid() -> str:
    return uuid.uuid4().hex


class PlanService:
    def __init__(self, settings, sessionmaker):
        self.s = settings
        self.sm = sessionmaker

    # --------------------------------------------------------------- CRUD
    async def list_plans(self) -> list[dict]:
        async with self.sm() as session:
            rows = (await session.execute(
                select(Plan).order_by(Plan.updated_at.desc())
            )).scalars().all()
            return [r.dict() for r in rows]

    async def get_plan(self, pid: str) -> dict | None:
        async with self.sm() as session:
            p = await session.get(Plan, pid)
            return p.dict() if p else None

    async def save_plan(self, data: dict) -> dict:
        """Create (no id / unknown id) or update an existing plan."""
        async with self.sm() as session:
            p = await session.get(Plan, data["id"]) if data.get("id") else None
            if p is None:
                p = Plan(id=data.get("id") or _uid())
                session.add(p)
            p.name = (data.get("name") or "Untitled plan").strip()
            p.object_name = data.get("object_name") or ""
            p.ra_deg = data.get("ra_deg")
            p.dec_deg = data.get("dec_deg")
            p.recipe_json = data.get("recipe") or []
            p.repeat = max(1, int(data.get("repeat") or 1))
            p.autofocus = bool(data.get("autofocus"))
            p.center = bool(data.get("center"))
            p.source = data.get("source") or "manual"
            p.updated_at = _utcnow()
            await session.commit()
            return p.dict()

    async def delete_plan(self, pid: str) -> None:
        async with self.sm() as session:
            p = await session.get(Plan, pid)
            if p:
                await session.delete(p)
                await session.commit()

    # ----------------------------------------------------------- expansion
    def _expand(self, plan: Plan, block_id: str) -> list[ExecutionStep]:
        steps: list[ExecutionStep] = []
        seq = 0

        def add(kind, params, state=StepState.PENDING.value):
            nonlocal seq
            steps.append(ExecutionStep(id=_uid(), block_id=block_id, seq=seq, kind=kind,
                                       params_json=params, state=state))
            seq += 1

        add("slew", {"ra_deg": plan.ra_deg, "dec_deg": plan.dec_deg,
                     "ra_hours": (plan.ra_deg / 15.0) if plan.ra_deg is not None else None})
        if plan.center:
            add("center", {"ra_deg": plan.ra_deg, "dec_deg": plan.dec_deg,
                           "ra_hours": (plan.ra_deg / 15.0) if plan.ra_deg is not None else None})
        if plan.autofocus:
            add("autofocus", {})

        recipe = plan.recipe_json or []
        total = int(plan.repeat) * sum(int(e.get("count", 1)) for e in recipe)
        frame = 0
        flat_prompted = False
        for _rep in range(int(plan.repeat)):
            for e in recipe:
                count = int(e.get("count", 1))
                image_type = (e.get("image_type") or "LIGHT").upper()
                # dithering + bias 0s only apply to certain frame types
                dither = int(e.get("dither_px", 0) or 0) if image_type == "LIGHT" else 0
                exptime = 0.0 if image_type == "BIAS" else float(e.get("exptime_s", 1.0))
                # flats need a human to set up the flat source — prompt once, before the first
                if image_type == "FLAT" and not flat_prompted:
                    add("prompt", {"message": "Flat frames next — set up your flat source "
                                              "(panel / twilight sky), then confirm."})
                    flat_prompted = True
                for _i in range(count):
                    frame += 1
                    add("expose", {
                        "exptime_s": exptime,
                        "image_type": image_type,
                        "filter_slot": e.get("filter_slot"),
                        "filter_name": e.get("filter_name"),
                        "binning": int(e.get("binning", 1) or 1),
                        "object_name": plan.object_name,
                        "dither_px": dither,
                        "frame": frame, "of": total,
                    })
                    if dither and frame < total:
                        add("dither", {"dither_px": dither})
        return steps

    # --------------------------------------------------------------- run
    async def run_plan(self, pid: str, mode: str = "attended", resume: bool = False) -> dict:
        async with self.sm() as session:
            plan = await session.get(Plan, pid)
            if plan is None:
                raise KeyError(pid)

            # resume: re-arm the last incomplete block (sequencer skips done steps)
            if resume and plan.last_block_id:
                block = await session.get(ExecutionBlock, plan.last_block_id)
                if block is not None and block.state in _INCOMPLETE:
                    steps = (await session.execute(
                        select(ExecutionStep).where(ExecutionStep.block_id == block.id)
                    )).scalars().all()
                    if any(st.state != StepState.DONE.value for st in steps):
                        block.state = BlockState.QUEUED.value
                        block.ended_at = None
                        for st in steps:
                            if st.state == StepState.FAILED.value:
                                st.state, st.error = StepState.PENDING.value, None
                        block.n_done = sum(1 for st in steps
                                           if st.state == StepState.DONE.value and st.kind == "expose")
                        plan.last_run_at = _utcnow()
                        await session.commit()
                        log.info("resuming plan %s → block %s", pid, block.id)
                        return {"block_id": block.id, "request_id": block.request_id, "resumed": True}

            # fresh run: new request + block + steps
            rid = _uid()
            req = ObservationRequest(
                id=rid, candidate_id=f"plan:{pid}",
                object_name=plan.object_name or plan.name,
                ra_deg=plan.ra_deg or 0.0, dec_deg=plan.dec_deg or 0.0,
                recipe_json=plan.recipe_json, mode=RunMode.ATTENDED.value,
                state=RequestState.QUEUED.value, created_by="plan",
            )
            next_seq = (await session.execute(
                select(ExecutionBlock).order_by(ExecutionBlock.seq.desc())
            )).scalars().first()
            block = ExecutionBlock(id=_uid(), request_id=rid, state=BlockState.QUEUED.value,
                                   seq=(next_seq.seq + 1) if next_seq else 0)
            steps = self._expand(plan, block.id)
            block.total_steps = len(steps)
            session.add(req)
            session.add(block)
            session.add_all(steps)
            plan.last_request_id = rid
            plan.last_block_id = block.id
            plan.last_run_at = _utcnow()
            await session.commit()
            log.info("plan %s → request %s, block %s (%d steps)", pid, rid, block.id, len(steps))
            return {"block_id": block.id, "request_id": rid, "resumed": False}
