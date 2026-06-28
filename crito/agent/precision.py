"""Precision pointing + focus operations (plate-solve center, HFR autofocus).

One service, single-flight (a lock): the manual endpoints fire-and-forget via
``start_center``/``start_autofocus`` (progress shows in telemetry), while the
executor ``await``s ``run_center``/``run_autofocus`` directly as block steps. Both
paths update the same progress dicts, so the UI shows either source the same way.

Centering: capture → ASTAP solve → if offset > tolerance, SYNC the mount to the
solved coords and re-slew to target; iterate. Autofocus: sweep the focuser, measure
HFR per step with ASTAP, fit a parabola, move to the minimum (final approach from
one side to absorb backlash).
"""
from __future__ import annotations

import asyncio
import io
import logging

from . import astap

log = logging.getLogger("crito.agent.precision")


def _fits_height(raw: bytes) -> int:
    try:
        from astropy.io import fits
        with fits.open(io.BytesIO(raw)) as h:
            return int(h[0].header.get("NAXIS2") or 0)
    except Exception:
        return 0


class PrecisionOps:
    def __init__(self, app):
        self.app = app
        self.s = app.state.settings
        self._lock = asyncio.Lock()
        self.center = self._idle_center()
        self.autofocus = self._idle_af()

    # ----------------------------------------------------------- state
    @staticmethod
    def _idle_center() -> dict:
        return {"running": False, "ok": None, "iterations": 0, "error_arcsec": None,
                "message": "idle", "solved": None}

    @staticmethod
    def _idle_af() -> dict:
        return {"running": False, "ok": None, "best_position": None, "best_hfr": None,
                "samples": [], "message": "idle"}

    def busy(self) -> bool:
        return self._lock.locked()

    def enabled(self) -> bool:
        return self.s.solver not in ("", "none")

    def snapshot(self) -> dict:
        return {"busy": self.busy(), "enabled": self.enabled(),
                "center": self.center, "autofocus": self.autofocus}

    def _act(self, msg: str, kind: str = "exec") -> None:
        a = getattr(self.app.state, "activity", None)
        if a is not None:
            a.push(msg, kind)

    # ---------------------------------------------------- manual entrypoints
    def start_center(self, ra_hours: float, dec_deg: float) -> None:
        asyncio.create_task(self._safe(self.run_center(ra_hours, dec_deg)))

    def start_autofocus(self) -> None:
        asyncio.create_task(self._safe(self.run_autofocus()))

    async def _safe(self, coro) -> None:
        try:
            await coro
        except Exception as e:
            log.exception("precision op failed")
            self._act(f"precision op failed: {e}", "error")

    # --------------------------------------------------------- plate-solve
    async def _solve_raw(self, raw: bytes):
        dm = self.app.state.dm
        fov = astap.compute_fov_deg(self.s.focal_length_mm, self.s.pixel_size_um, _fits_height(raw))
        st = dm.mount.status()
        with astap._TempFits(raw) as path:
            return await astap.solve(path, st.ra_hours, st.dec_deg, fov, self.s)

    async def run_center(self, ra_hours: float, dec_deg: float) -> dict:
        if not self.enabled():
            self.center = {**self._idle_center(), "ok": False, "message": "solver disabled"}
            return self.center
        async with self._lock:
            dm = self.app.state.dm
            if not (dm.connected and dm.mount and dm.camera):
                self.center = {**self._idle_center(), "ok": False, "message": "mount + camera required"}
                return self.center
            target_ra_deg = ra_hours * 15.0
            self.center = {"running": True, "ok": None, "iterations": 0,
                           "error_arcsec": None, "message": "solving", "solved": None}
            self._act(f"plate-solve + center on {ra_hours:.4f}h {dec_deg:+.3f}°")
            for i in range(1, self.s.center_max_iter + 1):
                self.center["iterations"] = i
                self.center["message"] = f"solving ({i}/{self.s.center_max_iter})"
                raw = await dm.camera.capture(self.s.solve_exposure_s)
                res = await self._solve_raw(raw)
                if res is None:
                    self.center["message"] = f"solve {i} failed"
                    self._act(f"plate-solve {i}: no solution", "alert")
                    continue
                sep = astap.angular_sep_arcsec(res.ra_deg, res.dec_deg, target_ra_deg, dec_deg)
                self.center["error_arcsec"] = round(sep, 1)
                self._act(f"plate-solve {i}: off by {sep:.0f}″")
                if sep <= self.s.center_tolerance_arcsec:
                    self.center.update(running=False, ok=True, message=f"centered ({sep:.0f}″)",
                                       solved={"ra_deg": res.ra_deg, "dec_deg": res.dec_deg})
                    self._act(f"✓ centered ({sep:.0f}″, {i} iter)")
                    return self.center
                await dm.mount.sync_to_radec(res.ra_deg / 15.0, res.dec_deg)
                await dm.mount.slew_to_radec(ra_hours, dec_deg, True)
                await self._await_slew(dm)
            self.center.update(running=False, ok=False, message="did not converge")
            self._act("✗ centering did not converge", "alert")
            return self.center

    async def solve_and_tag_wcs(self, fits_bytes: bytes) -> bytes:
        """Plate-solve a science frame and inject a TAN WCS into its header. Returns
        the bytes unchanged if the solver is disabled or the solve fails."""
        if not self.enabled():
            return fits_bytes
        try:
            res = await self._solve_raw(fits_bytes)
            if res is None:
                return fits_bytes
            from astropy.io import fits
            with fits.open(io.BytesIO(fits_bytes)) as hdul:
                h = hdul[0].header
                w, ht = int(h.get("NAXIS1") or 0), int(h.get("NAXIS2") or 0)
                for key, val, comment in astap.wcs_cards(res, w, ht):
                    h[key] = (val, comment)
                out = io.BytesIO()
                hdul.writeto(out, overwrite=True, output_verify="silentfix")
            return out.getvalue()
        except Exception:
            log.debug("WCS tag failed", exc_info=True)
            return fits_bytes

    # ----------------------------------------------------------- autofocus
    async def _analyse_raw(self, raw: bytes):
        with astap._TempFits(raw) as path:
            return await astap.analyse(path, self.s)

    async def run_autofocus(self) -> dict:
        if not self.enabled():
            self.autofocus = {**self._idle_af(), "ok": False, "message": "solver disabled"}
            return self.autofocus
        async with self._lock:
            dm = self.app.state.dm
            if not (dm.connected and dm.camera and dm.focuser):
                self.autofocus = {**self._idle_af(), "ok": False, "message": "camera + focuser required"}
                return self.autofocus
            f, cam, s = dm.focuser, dm.camera, self.s
            start = f.status().position
            if start is None:
                self.autofocus = {**self._idle_af(), "ok": False, "message": "focuser position unknown"}
                return self.autofocus
            start = int(start)
            span = s.af_step_size * (s.af_steps - 1)
            lo = start - span // 2
            positions = [lo + s.af_step_size * k for k in range(s.af_steps)]
            self.autofocus = {"running": True, "ok": None, "best_position": None,
                              "best_hfr": None, "samples": [], "message": "sweeping"}
            self._act(f"autofocus sweep {positions[0]}…{positions[-1]} ({s.af_steps} pts)")

            await self._move_focuser(f, lo - s.af_backlash)   # approach from below (backlash)
            samples = []
            for pos in positions:
                await self._move_focuser(f, pos)
                raw = await cam.capture(s.af_exposure_s)
                meas = await self._analyse_raw(raw)
                hfr, stars = (meas if meas else (None, 0))
                samples.append({"position": pos, "hfr": round(hfr, 2) if hfr else None, "stars": stars})
                self.autofocus["samples"] = list(samples)
                self.autofocus["message"] = f"{len(samples)}/{s.af_steps}: HFR {hfr if hfr else '—'}"
                self._act(f"autofocus @ {pos}: HFR {hfr if hfr else '—'} ({stars} stars)")

            valid = [(x["position"], x["hfr"]) for x in samples
                     if x["hfr"] and x["stars"] >= s.af_min_stars]
            best = None
            if len(valid) >= 3:
                best = astap.fit_parabola_min([v[0] for v in valid], [v[1] for v in valid])
            if best is None and valid:
                best = min(valid, key=lambda v: v[1])[0]      # fallback: best sampled point
            if best is None:
                self.autofocus.update(running=False, ok=False, message="autofocus failed — no stars")
                await self._move_focuser(f, start - s.af_backlash)
                await self._move_focuser(f, start)
                self._act("✗ autofocus failed — returned to start", "alert")
                return self.autofocus

            best = int(round(best))
            await self._move_focuser(f, best - s.af_backlash)  # final approach from below
            await self._move_focuser(f, best)
            meas = await self._analyse_raw(await cam.capture(s.af_exposure_s))
            final_hfr = round(meas[0], 2) if meas else None
            self.autofocus.update(running=False, ok=True, best_position=best, best_hfr=final_hfr,
                                  message=f"focused @ {best} (HFR {final_hfr})")
            self._act(f"✓ focused @ {best} (HFR {final_hfr})")
            return self.autofocus

    # ------------------------------------------------------------- waits
    async def _await_slew(self, dm, timeout: float = 180.0) -> None:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        await asyncio.sleep(0.5)
        while loop.time() < deadline:
            if not dm.mount.status().slewing:
                return
            await asyncio.sleep(0.5)

    async def _move_focuser(self, f, position: int, timeout: float = 60.0) -> None:
        await f.move_absolute(max(0, int(position)))
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        await asyncio.sleep(0.3)
        while loop.time() < deadline:
            if not f.status().moving:
                return
            await asyncio.sleep(0.3)
