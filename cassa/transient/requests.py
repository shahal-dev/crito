"""RequestBuilder — turn an approved candidate into an observation plan.

An approval creates one ``ObservationRequest`` (the recipe to carry out) plus one
``ExecutionBlock`` whose ordered ``ExecutionStep`` rows are the on-sky sequence:
slew → center (stub) → autofocus (stub) → expose ×N. The executor (Phase F) runs
the steps; the center/autofocus stubs are present so plate-solve/HFR slot in later
without a schema change.

Also owns the queue read/reorder used by the Execution Monitor.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select

from ..core.db import _utcnow
from ..core.transient_db import (
    BlockState,
    Candidate,
    ExecutionBlock,
    ExecutionStep,
    ObservationRequest,
    RequestState,
    RunMode,
    StepState,
)

log = logging.getLogger("cassa.transient.requests")

_ACTIVE_BLOCKS = (BlockState.QUEUED.value, BlockState.RUNNING.value, BlockState.PAUSED.value)


def _uid() -> str:
    return uuid.uuid4().hex


class RequestBuilder:
    def __init__(self, settings, sessionmaker):
        self.s = settings
        self.sm = sessionmaker

    def default_recipe(self) -> list[dict]:
        return [{
            "filter_slot": self.s.default_filter_slot,
            "exptime_s": float(self.s.default_exptime_s),
            "count": int(self.s.default_count),
            "dither_px": 0,
        }]

    def _expand_steps(self, req: ObservationRequest) -> list[ExecutionStep]:
        """Recipe → ordered steps. dm.capture sets the filter itself, so a filter
        slot rides on the expose step rather than a separate move step."""
        steps: list[ExecutionStep] = []
        seq = 0

        def add(kind, params, state=StepState.PENDING.value):
            nonlocal seq
            steps.append(ExecutionStep(id=_uid(), block_id="", seq=seq, kind=kind,
                                       params_json=params, state=state))
            seq += 1

        add("slew", {"ra_deg": req.ra_deg, "dec_deg": req.dec_deg,
                     "ra_hours": req.ra_deg / 15.0})
        add("center", {"note": "plate-solve TODO"})       # stub → executor skips
        add("autofocus", {"note": "HFR V-curve TODO"})    # stub → executor skips

        total = sum(int(e.get("count", 1)) for e in (req.recipe_json or []))
        frame = 0
        for entry in (req.recipe_json or []):
            for _ in range(int(entry.get("count", 1))):
                frame += 1
                add("expose", {
                    "exptime_s": float(entry.get("exptime_s", self.s.default_exptime_s)),
                    "filter_slot": entry.get("filter_slot"),
                    "object_name": req.object_name,
                    "dither_px": int(entry.get("dither_px", 0)),
                    "frame": frame, "of": total,
                })
        return steps

    async def build(self, candidate: dict, action: str, recipe=None, created_by="console") -> dict:
        """Create the request + execution block + steps for an approved candidate."""
        mode = RunMode.AUTO.value if action == "execute" else RunMode.ATTENDED.value
        recipe = recipe or self.default_recipe()
        rid = _uid()
        req = ObservationRequest(
            id=rid,
            candidate_id=candidate["id"],
            object_name=candidate.get("alert_id") or candidate["id"],
            ra_deg=candidate["ra_deg"], dec_deg=candidate["dec_deg"],
            recipe_json=recipe, mode=mode, priority=int(candidate.get("score") or 0),
            state=RequestState.QUEUED.value,
            window_start_utc=candidate.get("window_start_utc"),
            window_end_utc=candidate.get("window_end_utc"),
            created_by=created_by,
        )
        steps = self._expand_steps(req)
        async with self.sm() as session:
            next_seq = (await session.execute(
                select(func.coalesce(func.max(ExecutionBlock.seq), -1))
            )).scalar_one() + 1
            block = ExecutionBlock(id=_uid(), request_id=rid,
                                   state=BlockState.QUEUED.value, seq=next_seq,
                                   total_steps=len(steps))
            for st in steps:
                st.block_id = block.id
            session.add(req)
            session.add(block)
            session.add_all(steps)
            await session.commit()
            out = req.dict()
            out["block_id"] = block.id
        log.info("request %s (%s) → block %s, %d steps", rid, mode, block.id, len(steps))
        return out

    # ------------------------------------------------------------- queue read
    async def list_queue(self) -> list[dict]:
        """Active blocks (queued/running/paused) ordered by seq, enriched with the
        request + candidate context the Execution Monitor needs."""
        async with self.sm() as session:
            blocks = (await session.execute(
                select(ExecutionBlock)
                .where(ExecutionBlock.state.in_(_ACTIVE_BLOCKS))
                .order_by(ExecutionBlock.seq)
            )).scalars().all()
            out = []
            for b in blocks:
                d = b.dict()
                req = await session.get(ObservationRequest, b.request_id)
                if req:
                    d["request"] = req.dict()
                    cand = await session.get(Candidate, req.candidate_id)
                    d["class_label"] = cand.class_label if cand else None
                out.append(d)
            return out

    async def list_requests(self, limit: int = 100) -> list[dict]:
        async with self.sm() as session:
            rows = (await session.execute(
                select(ObservationRequest).order_by(ObservationRequest.created_at.desc()).limit(limit)
            )).scalars().all()
            return [r.dict() for r in rows]

    async def reorder_queue(self, block_ids: list[str]) -> list[dict]:
        async with self.sm() as session:
            for i, bid in enumerate(block_ids):
                b = await session.get(ExecutionBlock, bid)
                if b and b.state == BlockState.QUEUED.value:
                    b.seq = i
            await session.commit()
        return await self.list_queue()
