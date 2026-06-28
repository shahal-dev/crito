"""Visibility engine — pure astropy, no hardware, no astroplan.

Given a sky position and the site, compute whether the target clears the altitude
limit during tonight's astronomical-dark window, and (if so) the observable window,
peak altitude, minimum airmass, and moon separation/illumination.

Everything is tz-aware UTC. "Tonight" is the local night bracketing the next local
midnight, derived from the site's ``utc_offset_hours``. The night grid is computed
once per night and reused for every candidate (cheap, vectorized).

This module is stateless and unit-testable offline — the key seam for testing the
pipeline without a broker or a telescope.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import numpy as np
from astropy import units as u
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, get_sun
from astropy.time import Time

_UNIX_MJD = 40587.0  # MJD at the unix epoch (1970-01-01)


def now_mjd() -> float:
    return dt.datetime.now(dt.timezone.utc).timestamp() / 86400.0 + _UNIX_MJD


@dataclass
class NightWindow:
    """Tonight's astronomical-dark window + the dark-time sample grid.

    Moon/Sun positions over the dark grid are precomputed once so per-object
    visibility is just one alt transform + cheap array ops (fast for the whole feed).
    """

    ut_date: str            # evening local civil date, YYYYMMDD
    start_utc: str          # dark start (ISO, UTC)
    end_utc: str            # dark end (ISO, UTC)
    twilight_used: float    # -18 (astronomical) normally, -12 (nautical) fallback
    times: Time             # the dark sub-grid, reused per candidate
    moon: SkyCoord = None    # Moon (ICRS) over the dark grid
    sun: SkyCoord = None     # Sun (ICRS) over the dark grid

    def info(self) -> dict:
        return {
            "ut_date": self.ut_date,
            "start_utc": self.start_utc,
            "end_utc": self.end_utc,
            "twilight_used": self.twilight_used,
            "n_samples": len(self.times),
        }


@dataclass
class VisibilityResult:
    observable: bool
    window_start_utc: str | None = None
    window_end_utc: str | None = None
    max_alt_deg: float | None = None
    min_airmass: float | None = None
    moon_sep_deg: float | None = None
    moon_illum_frac: float | None = None

    def dict(self) -> dict:
        return {
            "observable": self.observable,
            "window_start_utc": self.window_start_utc,
            "window_end_utc": self.window_end_utc,
            "max_alt_deg": self.max_alt_deg,
            "min_airmass": self.min_airmass,
            "moon_sep_deg": self.moon_sep_deg,
            "moon_illum_frac": self.moon_illum_frac,
        }


def site_location(location) -> EarthLocation:
    """Build an EarthLocation from an observatory ``Location`` (or any object with
    latitude_deg/longitude_deg/elevation_m)."""
    return EarthLocation(
        lat=float(location.latitude_deg) * u.deg,
        lon=float(location.longitude_deg) * u.deg,
        height=float(getattr(location, "elevation_m", 0.0) or 0.0) * u.m,
    )


def night_label(when_utc: dt.datetime, utc_offset_hours: float) -> str:
    """The evening-local civil date (YYYYMMDD) that labels the night containing
    ``when_utc``. Before local noon counts as the previous evening's night."""
    local = when_utc + dt.timedelta(hours=utc_offset_hours)
    evening = local.date() if local.hour >= 12 else (local - dt.timedelta(days=1)).date()
    return evening.strftime("%Y%m%d")


def _dark_run(sun_alt_deg: np.ndarray, twilight: float) -> tuple[int, int] | None:
    """Indices [i, j] of the contiguous below-twilight run containing the deepest
    point of night, or None if the Sun never drops below ``twilight``."""
    mask = sun_alt_deg < twilight
    if not mask.any():
        return None
    k = int(np.argmin(sun_alt_deg))
    if not mask[k]:                                  # deepest point above threshold
        k = int(np.where(mask)[0][len(np.where(mask)[0]) // 2])
    i = k
    while i - 1 >= 0 and mask[i - 1]:
        i -= 1
    j = k
    while j + 1 < len(mask) and mask[j + 1]:
        j += 1
    return i, j


def compute_night(
    location: EarthLocation,
    when_utc: dt.datetime,
    utc_offset_hours: float,
    step_min: float = 5.0,
) -> NightWindow:
    """Astronomical-dark window for the night containing ``when_utc``.

    Falls back to nautical twilight (-12°) if the Sun never reaches -18° (it does
    at Dhaka, but the guard keeps the engine total). The returned ``times`` grid is
    the dark sub-window, so a target's alt-mask over it already excludes twilight.
    """
    local = when_utc + dt.timedelta(hours=utc_offset_hours)
    noon_local = local.replace(hour=12, minute=0, second=0, microsecond=0)
    if local.hour < 12:
        noon_local -= dt.timedelta(days=1)
    start_local = noon_local                          # local noon → next local noon
    start_utc = (start_local - dt.timedelta(hours=utc_offset_hours)).replace(tzinfo=None)

    n = int(round(24 * 60 / step_min)) + 1
    grid = Time(start_utc, scale="utc") + np.arange(n) * step_min * u.min
    frame = AltAz(obstime=grid, location=location)
    sun_alt = get_sun(grid).transform_to(frame).alt.deg

    twilight = -18.0
    run = _dark_run(sun_alt, twilight)
    if run is None:
        twilight = -12.0
        run = _dark_run(sun_alt, twilight)
    if run is None:                                   # polar/degenerate guard
        twilight = 0.0
        run = _dark_run(sun_alt, twilight) or (0, n - 1)

    i, j = run
    dark = grid[i:j + 1]
    # precompute Moon/Sun over the dark grid once for cheap per-object reuse. Keep the
    # apparent GCRS positions — NOT .icrs, which re-centers to the barycenter and ruins
    # the direction of a finite-distance body (separations would be wrong).
    moon = get_body("moon", dark, location)
    sun = get_sun(dark)
    return NightWindow(
        ut_date=night_label(when_utc, utc_offset_hours),
        start_utc=grid[i].isot,
        end_utc=grid[j].isot,
        twilight_used=twilight,
        times=dark,
        moon=moon,
        sun=sun,
    )


def observability(
    ra_deg: float,
    dec_deg: float,
    night: NightWindow,
    location: EarthLocation,
    alt_min: float = 30.0,
    best_min: float = 60.0,
) -> dict:
    """Rich tonight's-observability for a target: the dark-time window it spends above
    ``alt_min`` (observable), above ``best_min`` (best), its peak altitude + time, and
    closest moon separation. All windows are within tonight's astronomical-dark window.
    """
    times = night.times
    frame = AltAz(obstime=times, location=location)
    target = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    alt = np.asarray(target.transform_to(frame).alt.deg)
    kmax = int(np.argmax(alt))
    max_alt = float(alt[kmax])

    def _win(thr: float):
        idx = np.where(alt >= thr)[0]
        if not len(idx):
            return None, None
        return times[idx[0]].isot + "Z", times[idx[-1]].isot + "Z"

    obs_s, obs_e = _win(alt_min)
    best_s, best_e = _win(best_min)
    idx = np.where(alt >= alt_min)[0]
    moon_sep = (round(float(np.min(target.separation(night.moon[idx]).deg)), 1)
                if len(idx) and night.moon is not None else None)
    return {
        "observable": max_alt >= alt_min,
        "alt_min": alt_min,
        "best_min": best_min,
        "window_start_utc": obs_s,
        "window_end_utc": obs_e,
        "best_start_utc": best_s,
        "best_end_utc": best_e,
        "max_alt_deg": round(max_alt, 1),
        "max_alt_utc": times[kmax].isot + "Z",
        "moon_sep_deg": moon_sep,
        "night_start_utc": night.start_utc + "Z",
        "night_end_utc": night.end_utc + "Z",
        "twilight_used": night.twilight_used,
    }


def visibility(
    ra_deg: float,
    dec_deg: float,
    night: NightWindow,
    location: EarthLocation,
    alt_min_deg: float = 30.0,
) -> VisibilityResult:
    """Is (ra, dec) above ``alt_min_deg`` at any point during tonight's dark window?

    Returns the observable sub-window, peak altitude, min airmass, and the closest
    moon separation across the window (conservative) plus mean lunar illumination.
    """
    times = night.times
    frame = AltAz(obstime=times, location=location)
    target = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
    alt = target.transform_to(frame).alt.deg

    # peak altitude over the dark window is always reported, so non-observable
    # objects still show *why* (e.g. peak 14° — below the 30° limit).
    max_alt = float(np.max(alt))
    res = VisibilityResult(observable=max_alt >= alt_min_deg, max_alt_deg=round(max_alt, 2))
    if not res.observable:
        return res

    idx = np.where(alt >= alt_min_deg)[0]
    res.window_start_utc = times[idx[0]].isot
    res.window_end_utc = times[idx[-1]].isot
    res.min_airmass = round(1.0 / math.sin(math.radians(max_alt)), 3) if max_alt > 0 else None
    # Moon/Sun precomputed on the night grid (ICRS) → cheap separation/illumination.
    moon_win = night.moon[idx]
    res.moon_sep_deg = round(float(np.min(target.separation(moon_win).deg)), 1)
    elong = night.sun[idx].separation(moon_win).radian
    res.moon_illum_frac = round(float(np.mean((1.0 - np.cos(elong)) / 2.0)), 3)
    return res
