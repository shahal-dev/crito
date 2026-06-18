"""Weather + safety state machine — the gate for unattended/auto operation.

A background monitor evaluates a SAFE → WARN → UNSAFE → FAULT state every second
from weather conditions, e-stop, and (optionally) Sun altitude:

  - UNSAFE/FAULT are applied immediately (fail-fast): rain, high wind/humidity,
    stale or missing weather data, a weather-station alert, or an e-stop.
  - Returning to SAFE requires conditions to hold OK for ``safety_clear_delay_s``
    (hysteresis, so it doesn't flap).
  - On entering UNSAFE/FAULT the monitor takes protective action: abort the running
    sequence and PARK the mount (unless override is on).

Weather comes from whichever is available: readings pushed to ``POST /api/safety/
weather`` (any source — a sensor script, a weather API), or an INDI weather device
(``CASSA_WEATHER_DEVICE``). No data + enforcement on = UNSAFE, which correctly
blocks unattended observing until a real source exists. The executor consults this
monitor before dispatching and is aborted by it on UNSAFE.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("cassa.agent.safety")

_LEVELS = ("safe", "warn", "unsafe", "fault")


class SafetyMonitor:
    def __init__(self, app):
        self.app = app
        self.s = app.state.settings
        self.state = "unsafe"          # cautious until proven safe
        self.reasons: list[str] = ["initializing"]
        self.weather: dict = {}         # latest pushed readings (+ updated_at)
        self.override = False           # disable enforcement (operator's call; dangerous)
        self.estop = False              # latched emergency stop → FAULT
        self._ok_since: float | None = None
        self._sun_alt: float | None = None
        self._sun_at = 0.0
        self._stop = False
        self._task: asyncio.Task | None = None

    # ----------------------------------------------------------- inputs
    def set_weather(self, data: dict) -> None:
        self.weather = {**{k: v for k, v in data.items() if v is not None},
                        "updated_at": time.time(), "source": data.get("source", "manual")}

    def estop_trip(self) -> None:
        self.estop = True

    def estop_clear(self) -> None:
        self.estop = False

    def set_override(self, on: bool) -> None:
        self.override = bool(on)

    # ----------------------------------------------------- weather sources
    def _indi_weather(self) -> dict | None:
        """Read an INDI weather device if configured (driver does its own thresholds
        via WEATHER_STATUS Ok/Busy/Alert)."""
        dev = self.s.weather_device
        if not dev:
            return None
        try:
            client = self.app.state.dm.client
            if not client.has_prop(dev, "WEATHER_STATUS"):
                return None
            out = {"updated_at": time.time(), "source": "indi",
                   "indi_status": client.prop_state(dev, "WEATHER_STATUS")}
            for elem, key in (("WEATHER_HUMIDITY", "humidity"), ("WEATHER_WIND_SPEED", "wind_speed"),
                              ("WEATHER_TEMPERATURE", "temperature"), ("WEATHER_RAIN_HOUR", "rain"),
                              ("WEATHER_CLOUD_COVER", "clouds")):
                v = client.element(dev, "WEATHER_PARAMETERS", elem)
                if v is not None:
                    out[key] = v
            return out
        except Exception:
            log.debug("INDI weather read failed", exc_info=True)
            return None

    def _sun_altitude(self) -> float | None:
        """Sun altitude (deg) at the site, recomputed at most every 60 s."""
        now = time.time()
        if now - self._sun_at < 60 and self._sun_alt is not None:
            return self._sun_alt
        loc = self.app.state.observatory.location
        if not loc or not getattr(loc, "is_set", False):
            return None
        try:
            import astropy.units as u
            from astropy.coordinates import AltAz, EarthLocation, get_sun
            from astropy.time import Time
            el = EarthLocation(lat=loc.latitude_deg * u.deg, lon=loc.longitude_deg * u.deg,
                               height=(loc.elevation_m or 0) * u.m)
            t = Time.now()
            self._sun_alt = float(get_sun(t).transform_to(AltAz(obstime=t, location=el)).alt.deg)
            self._sun_at = now
            return self._sun_alt
        except Exception:
            return None

    # ----------------------------------------------------------- evaluate
    def _evaluate(self) -> tuple[str, list[str]]:
        if self.estop:
            return "fault", ["emergency stop"]
        if not self.s.safety_enabled:
            return "safe", ["safety disabled"]

        reasons: list[str] = []
        level = 0

        def bump(lvl, why):
            nonlocal level
            level = max(level, lvl)
            reasons.append(why)

        w = self._indi_weather() or (self.weather or None)
        if not w:
            bump(2, "no weather data")
        else:
            age = time.time() - w.get("updated_at", 0)
            if age > self.s.safety_stale_s:
                bump(2, f"weather stale ({int(age)}s)")
            st = w.get("indi_status")
            if st == "Alert":
                bump(2, "weather station alert")
            elif st == "Busy":
                bump(1, "weather station caution")
            if w.get("rain"):
                bump(2, "rain")
            hum = w.get("humidity")
            if hum is not None:
                if hum >= self.s.safety_humidity_unsafe:
                    bump(2, f"humidity {hum:g}%")
                elif hum >= self.s.safety_humidity_warn:
                    bump(1, f"humidity {hum:g}%")
            wind = w.get("wind_speed")
            if wind is not None and wind >= self.s.safety_wind_unsafe:
                bump(2, f"wind {wind:g}")
            clouds = w.get("clouds")
            if clouds is not None and clouds >= self.s.safety_cloud_unsafe:
                bump(2, f"clouds {clouds:g}%")

        alt = self._sun_altitude()
        if alt is not None and alt > 0:
            bump(1, "daylight")

        return _LEVELS[level], reasons

    # --------------------------------------------------------------- loop
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        await asyncio.sleep(2.0)
        while not self._stop:
            try:
                raw, reasons = self._evaluate()
                eff, reasons = self._apply_hysteresis(raw, reasons)
                if eff != self.state:
                    await self._on_transition(self.state, eff, reasons)
                    self.state = eff
                self.reasons = reasons or ["ok"]
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("safety evaluation failed")
            await asyncio.sleep(1.0)

    def _apply_hysteresis(self, raw: str, reasons: list[str]) -> tuple[str, list[str]]:
        if raw in ("unsafe", "fault"):
            self._ok_since = None
            return raw, reasons
        # conditions are not unsafe — track how long they've been OK
        now = time.time()
        if self._ok_since is None:
            self._ok_since = now
        if raw == "warn":
            return "warn", reasons
        cleared = (now - self._ok_since) >= self.s.safety_clear_delay_s
        if self.state == "safe" or cleared:
            return "safe", reasons
        remain = int(self.s.safety_clear_delay_s - (now - self._ok_since))
        return "warn", [f"stabilizing after unsafe ({remain}s)"]

    async def _on_transition(self, old: str, new: str, reasons: list[str]) -> None:
        act = getattr(self.app.state, "activity", None)
        protective = new in ("unsafe", "fault") and old not in ("unsafe", "fault")
        if protective and not self.override:
            msg = f"SAFETY {new.upper()}: {', '.join(reasons)} — aborting + parking"
            log.warning(msg)
            if act:
                act.push(msg, "error")
            try:
                await self.app.state.executor.abort()
            except Exception:
                log.debug("safety abort failed", exc_info=True)
            try:
                dm = self.app.state.dm
                if dm.connected and dm.mount is not None:
                    await dm.mount.set_tracking(False)
                    await dm.mount.park(True)
            except Exception:
                log.debug("safety park failed", exc_info=True)
            notifier = getattr(self.app.state, "notifier", None)
            if notifier is not None:
                try:
                    await notifier.email.send("[CASSA] SAFETY " + new.upper(), msg)
                except Exception:
                    pass
        elif act:
            act.push(f"safety {old} → {new}", "alert" if new != "safe" else "info")

    # -------------------------------------------------------- gating + view
    def ok_to_dispatch(self, mode: str) -> bool:
        """Whether the executor may start a block. Auto needs SAFE; attended is
        blocked only when UNSAFE/FAULT."""
        if self.override or not self.s.safety_enabled:
            return True
        if mode == "auto":
            return self.state == "safe"
        return self.state not in ("unsafe", "fault")

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "reasons": self.reasons,
            "override": self.override,
            "estop": self.estop,
            "enabled": bool(self.s.safety_enabled),
            "weather": self._indi_weather() or self.weather or {},
            "sun_alt": self._sun_alt,
        }
