"""ExecutionSequencer — run approved observation blocks on-sky.

A single-flight background task (one mount → one block at a time). For each block
it runs the ordered steps by calling the SAME async device methods the manual
endpoints use, so it inherits all the INDI handling and stays on the event loop:

    slew → center (stub) → autofocus (stub) → expose ×N  (each: dm.capture → archive.ingest)

Live progress is exposed via ``snapshot()`` and merged into the existing telemetry
WebSocket frame — no new socket. Control verbs (launch/pause/resume/abort) set
flags the loop checks between steps. ``manual_override`` (set by any manual device
endpoint) stops auto-dispatch and pauses a running block — manual control wins.

Run modes:
  • attended (Approve→Queue): runs only when the operator ``launch()``-es the block.
  • auto      (Approve→Execute): auto-dispatched when inside the observable window
    AND the global ``CASSA_AUTO_EXECUTE`` master switch is on AND no manual override.
    OFF by default until a weather/safety FSM exists — then it just waits for a
    manual launch like an attended block.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import random

from sqlalchemy import select

from ..core.db import _utcnow
from ..core.transient_db import (
    BlockState,
    ExecutionBlock,
    ExecutionStep,
    ObservationRequest,
    RequestState,
    RunMode,
    StepState,
)

log = logging.getLogger("cassa.transient.executor")


def _idle() -> dict:
    return {"state": "idle", "block_id": None, "object": None, "mode": None,
            "step": None, "current_step": 0, "total": 0, "n_done": 0, "n_failed": 0,
            "exposure_remaining": 0.0, "awaiting_confirm": None}


class ExecutionSequencer:
    def __init__(self, app):
        self.app = app
        self.s = app.state.settings
        self._stop = False
        self.manual_override = False
        self._pause = False
        self._abort_block = False
        self._confirmed = False
        self._launched: set[str] = set()
        self.progress = _idle()

    # ----------------------------------------------------------- control verbs
    def launch(self, block_id: str) -> None:
        self._launched.add(block_id)

    def pause(self) -> None:
        self._pause = True

    def resume(self) -> None:
        self._pause = False
        self.manual_override = False  # an explicit resume also clears manual hold

    def confirm(self) -> None:
        """Operator acknowledged a prompt (e.g. flat setup) — proceed, clearing any
        manual hold they set while preparing."""
        self._confirmed = True
        self._pause = False
        self.manual_override = False

    async def abort(self) -> None:
        self._abort_block = True
        dm = self.app.state.dm
        try:
            if dm.connected and dm.mount:
                await dm.mount.abort()
        except Exception:
            log.debug("mount abort during sequence abort failed", exc_info=True)

    def note_manual(self) -> None:
        """Called by manual device endpoints — manual control preempts the queue."""
        self.manual_override = True

    def _act(self, msg: str, kind: str = "exec") -> None:
        a = getattr(self.app.state, "activity", None)
        if a is not None:
            a.push(msg, kind)

    def set_override(self, on: bool) -> None:
        self.manual_override = on

    # --------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        p = dict(self.progress)
        p["manual_override"] = self.manual_override
        p["auto_execute"] = bool(self.s.auto_execute)
        dm = self.app.state.dm
        try:
            if p.get("state") == "running" and dm.camera is not None:
                p["exposure_remaining"] = dm.camera.status().exposure_remaining
        except Exception:
            pass
        return p

    # ------------------------------------------------------------- main loop
    async def run(self) -> None:
        await asyncio.sleep(2.0)
        while not self._stop:
            try:
                block_id = await self._next_runnable()
                if block_id is None:
                    if self.progress["state"] != "idle":
                        self.progress = _idle()
                    await asyncio.sleep(2.0)
                    continue
                await self._run_block(block_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("execution loop error")
                await asyncio.sleep(2.0)

    async def stop(self) -> None:
        self._stop = True

    def _devices_ready(self) -> bool:
        dm = self.app.state.dm
        return bool(dm.connected and dm.mount and dm.camera)

    def _within_window(self, req: ObservationRequest) -> bool:
        if not (req.window_start_utc and req.window_end_utc):
            return True
        try:
            start = dt.datetime.fromisoformat(req.window_start_utc)
            end = dt.datetime.fromisoformat(req.window_end_utc)
        except ValueError:
            return True
        # normalize to aware UTC (astropy isot is naive UTC) for a safe comparison
        aware = lambda d: d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
        now = dt.datetime.now(dt.timezone.utc)
        return aware(start) <= now <= aware(end)

    async def _next_runnable(self) -> str | None:
        if self.manual_override or not self._devices_ready():
            return None
        safety = getattr(self.app.state, "safety", None)
        sm = self.app.state.db.sessionmaker
        async with sm() as s:
            blocks = (await s.execute(
                select(ExecutionBlock)
                .where(ExecutionBlock.state == BlockState.QUEUED.value)
                .order_by(ExecutionBlock.seq)
            )).scalars().all()
            for b in blocks:
                if b.id in self._launched:                        # attended launch
                    if safety is None or safety.ok_to_dispatch("attended"):
                        return b.id
                    continue                                      # launched but unsafe → hold
                req = await s.get(ObservationRequest, b.request_id)
                if (req and req.mode == RunMode.AUTO.value and self.s.auto_execute
                        and self._within_window(req)
                        and (safety is None or safety.ok_to_dispatch("auto"))):
                    return b.id                                   # guarded auto-dispatch
        return None

    # --------------------------------------------------------------- run block
    async def _run_block(self, block_id: str) -> None:
        sm = self.app.state.db.sessionmaker
        dm = self.app.state.dm
        archive = self.app.state.archive

        async with sm() as s:
            block = await s.get(ExecutionBlock, block_id)
            req = await s.get(ObservationRequest, block.request_id)
            steps = (await s.execute(
                select(ExecutionStep).where(ExecutionStep.block_id == block_id)
                .order_by(ExecutionStep.seq)
            )).scalars().all()
            block.state = BlockState.RUNNING.value
            block.started_at = _utcnow()
            plan = [(st.id, st.seq, st.kind, dict(st.params_json or {}), st.state) for st in steps]
            total, object_name, mode = block.total_steps, req.object_name, req.mode
            req_ra_deg, req_dec_deg = req.ra_deg, req.dec_deg
            await s.commit()

        log.info("executing block %s (%s, %d steps) → %s", block_id, mode, total, object_name)
        self._act(f"▶ observing {object_name} ({mode}, {total} steps)")
        self._abort_block = False
        n_done = n_failed = 0
        aborted = False
        self.progress = {"state": "running", "block_id": block_id, "object": object_name,
                         "mode": mode, "step": None, "current_step": 0, "total": total,
                         "n_done": 0, "n_failed": 0, "exposure_remaining": 0.0,
                         "awaiting_confirm": None}

        for sid, seq, kind, params, state in plan:
            # resume: a re-launched block skips steps that already finished
            if state in (StepState.DONE.value, StepState.SKIPPED.value):
                if kind == "expose" and state == StepState.DONE.value:
                    n_done += 1
                continue
            await self._gate()
            if self._abort_block or self._stop:
                aborted = True
                break
            self.progress["current_step"] = seq
            self.progress["step"] = self._label(kind, params)
            await self._set_step(sid, StepState.RUNNING)
            try:
                if kind == "slew":
                    if params.get("ra_hours") is None:
                        await self._set_step(sid, StepState.SKIPPED)   # no coords (shouldn't happen)
                    else:
                        await dm.mount.slew_to_radec(params["ra_hours"], params["dec_deg"], True)
                        await self._await_slew(dm)
                        await self._set_step(sid, StepState.DONE)
                elif kind == "center":
                    precision = getattr(self.app.state, "precision", None)
                    ra_h = params.get("ra_hours")
                    dec = params.get("dec_deg")
                    if ra_h is None and req_ra_deg is not None:
                        ra_h, dec = req_ra_deg / 15.0, req_dec_deg
                    if precision is None or not precision.enabled() or ra_h is None:
                        await self._set_step(sid, StepState.SKIPPED)
                    else:
                        res = await precision.run_center(ra_h, dec)
                        # a failed center is non-fatal: the slew already put us close,
                        # so flag it but keep imaging rather than abandon the block.
                        await self._set_step(sid, StepState.DONE if res.get("ok") else StepState.FAILED,
                                             error=None if res.get("ok") else res.get("message"))
                elif kind == "autofocus":
                    precision = getattr(self.app.state, "precision", None)
                    if (precision is None or not precision.enabled()
                            or self.app.state.dm.focuser is None):
                        await self._set_step(sid, StepState.SKIPPED)
                    else:
                        res = await precision.run_autofocus()
                        await self._set_step(sid, StepState.DONE if res.get("ok") else StepState.FAILED,
                                             error=None if res.get("ok") else res.get("message"))
                elif kind == "dither":
                    await self._dither(dm, params.get("dither_px", 0))
                    await self._set_step(sid, StepState.DONE)
                elif kind == "prompt":
                    await self._await_confirm(params.get("message", "Confirm to continue"))
                    await self._set_step(sid, StepState.DONE if not self._abort_block else StepState.SKIPPED)
                elif kind == "expose":
                    itype = params.get("image_type", "LIGHT")
                    filter_slot = params.get("filter_slot")
                    if itype in ("DARK", "BIAS"):
                        # block light with the opaque/"dark" filter slot (QHY MiniCam8 wheel)
                        filter_slot = self._dark_filter_slot() or filter_slot
                    authored = await dm.capture(params["exptime_s"], itype,
                                                object_name if itype == "LIGHT" else "",
                                                filter_slot, binning=params.get("binning"))
                    fits_bytes, meta = authored["fits"], authored["meta"]
                    precision = getattr(self.app.state, "precision", None)
                    if (itype == "LIGHT" and getattr(self.s, "solve_science_frames", False)
                            and precision is not None and precision.enabled()):
                        tagged = await precision.solve_and_tag_wcs(fits_bytes)
                        if tagged is not fits_bytes:                # solved → WCS written
                            import hashlib
                            fits_bytes = tagged
                            meta = {**meta, "sha256": hashlib.sha256(tagged).hexdigest()}
                    rec = await archive.ingest(fits_bytes, meta)
                    await self._set_step(sid, StepState.DONE, image_id=rec["id"])
                    n_done += 1
                    self.progress["n_done"] = n_done
                else:
                    await self._set_step(sid, StepState.SKIPPED)
            except Exception as e:
                log.exception("block %s step %s (%s) failed", block_id, sid, kind)
                await self._set_step(sid, StepState.FAILED, error=str(e))
                n_failed += 1
                self.progress["n_failed"] = n_failed
                if kind == "slew":
                    aborted = True                  # no pointing → can't image
                    break

        async with sm() as s:
            block = await s.get(ExecutionBlock, block_id)
            req = await s.get(ObservationRequest, block.request_id)
            block.n_done, block.n_failed = n_done, n_failed
            block.ended_at = _utcnow()
            if aborted:
                block.state = BlockState.ABORTED.value
            elif n_done == 0 and n_failed:
                block.state = BlockState.FAILED.value
            else:
                block.state = BlockState.DONE.value
            if req:
                req.state = RequestState.DONE.value
            await s.commit()
            final = block.state

        self._launched.discard(block_id)
        self._abort_block = False
        self.progress = _idle()
        log.info("block %s finished: %s (%d done, %d failed)", block_id, final, n_done, n_failed)
        self._act(f"{final}: {object_name} — {n_done} frame(s) archived",
                  "alert" if final != "done" else "exec")

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _label(kind: str, params: dict) -> str:
        if kind == "expose":
            t = (params.get("image_type") or "light").lower()
            if params.get("of"):
                return f"{t} {params.get('frame')}/{params.get('of')}"
            return t
        return kind

    async def _gate(self) -> None:
        """Block while paused or under manual override (unless aborting/stopping)."""
        waiting = False
        while (self._pause or self.manual_override) and not (self._abort_block or self._stop):
            if not waiting:
                self.progress["state"] = "paused"
                waiting = True
            await asyncio.sleep(0.4)
        if waiting and not (self._abort_block or self._stop):
            self.progress["state"] = "running"

    async def _await_confirm(self, message: str) -> None:
        """Block the sequence until the operator confirms (e.g. flat-frame setup)."""
        self._confirmed = False
        self.progress["awaiting_confirm"] = message
        self.progress["state"] = "paused"
        self._act(f"⏸ waiting for operator: {message}", "alert")
        while not self._confirmed and not (self._abort_block or self._stop):
            await asyncio.sleep(0.4)
        self.progress["awaiting_confirm"] = None
        if not (self._abort_block or self._stop):
            self.progress["state"] = "running"
            self._act("▶ operator confirmed — continuing")

    def _dark_filter_slot(self) -> int | None:
        """The opaque 'dark' filter slot for dark/bias frames: an explicit config
        slot, else a slot named dark/blank/opaque/shutter on the wheel."""
        slot = getattr(self.s, "dark_filter_slot", 0) or 0
        if slot:
            return int(slot)
        fw = self.app.state.dm.filterwheel
        if fw is not None:
            try:
                names = fw.status().names or []
            except Exception:
                names = []
            for i, nm in enumerate(names, 1):
                if any(k in (nm or "").lower() for k in ("dark", "blank", "opaque", "shutter")):
                    return i
        return None

    async def _dither(self, dm, dither_px) -> None:
        """Nudge the mount by a small random offset (px treated as arcsec until a
        plate-solve pixel scale is wired in). Keeps the star off the same pixels."""
        try:
            amp = (float(dither_px) or 0.0) / 3600.0  # arcsec → deg
        except (TypeError, ValueError):
            return
        if not amp or dm.mount is None:
            return
        st = dm.mount.status()
        if st.ra_hours is None or st.dec_deg is None:
            return
        cosd = max(0.1, math.cos(math.radians(st.dec_deg)))
        new_ra = (st.ra_hours + random.uniform(-1, 1) * amp / 15.0 / cosd) % 24.0
        new_dec = max(-89.0, min(89.0, st.dec_deg + random.uniform(-1, 1) * amp))
        await dm.mount.slew_to_radec(new_ra, new_dec, True)
        await self._await_slew(dm)

    async def _await_slew(self, dm, timeout: float = 180.0) -> None:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        await asyncio.sleep(0.5)                     # let the slew flag go Busy
        while loop.time() < deadline:
            if self._abort_block:
                return
            if not dm.mount.status().slewing:
                return
            await asyncio.sleep(0.5)
        raise TimeoutError("slew did not complete within timeout")

    async def _set_step(self, step_id: str, state: StepState, image_id=None, error=None) -> None:
        sm = self.app.state.db.sessionmaker
        async with sm() as s:
            st = await s.get(ExecutionStep, step_id)
            if not st:
                return
            if st.state == StepState.PENDING.value or state != StepState.RUNNING:
                st.state = state.value
            if state == StepState.RUNNING:
                st.started_at = _utcnow()
            else:
                st.ended_at = _utcnow()
            if image_id:
                st.image_id = image_id
            if error:
                st.error = error
            await s.commit()
