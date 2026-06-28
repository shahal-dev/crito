"""Night almanac — sun/twilight times, astronomical night, and moon phase/rise/set.

Pure astropy, reusing the visibility engine's site location. Computes tonight's full
schedule (sunset/sunrise, civil/nautical/astronomical twilight, the astronomical-dark
window) by sampling the Sun's altitude over a local-noon→noon grid and interpolating
the threshold crossings, plus the Moon's current phase/illumination and next rise/set.

Stateless and slow-changing — the API caches it.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
from astropy import units as u
from astropy.coordinates import AltAz, get_body, get_sun
from astropy.time import Time

from .visibility import site_location

# standard horizon dips
_SUN_HORIZON = -0.833   # refraction + solar radius
_MOON_HORIZON = -0.5


def _interp(tu, alt, i: int, thr: float) -> float:
    a0, a1 = alt[i], alt[i + 1]
    frac = 0.0 if a0 == a1 else (a0 - thr) / (a0 - a1)
    return float(tu[i] + (tu[i + 1] - tu[i]) * frac)


def _first_desc(tu, alt, thr: float):
    """First above→below crossing of ``thr`` (e.g. sunset, dusk, moonset)."""
    for i in range(len(alt) - 1):
        if alt[i] >= thr > alt[i + 1]:
            return _interp(tu, alt, i, thr)
    return None


def _first_asc(tu, alt, thr: float):
    """First below→above crossing of ``thr`` (e.g. sunrise, dawn, moonrise)."""
    for i in range(len(alt) - 1):
        if alt[i] < thr <= alt[i + 1]:
            return _interp(tu, alt, i, thr)
    return None


def _iso(unix):
    if unix is None:
        return None
    return dt.datetime.fromtimestamp(unix, tz=dt.timezone.utc).isoformat()


def _phase_name(illum: float, waxing: bool) -> str:
    if illum < 0.04:
        return "New Moon"
    if illum > 0.96:
        return "Full Moon"
    if 0.46 <= illum <= 0.54:
        return "First Quarter" if waxing else "Last Quarter"
    band = "Crescent" if illum < 0.5 else "Gibbous"
    return f"{'Waxing' if waxing else 'Waning'} {band}"


def _phase_emoji(illum: float, waxing: bool) -> str:
    if illum < 0.04:
        return "🌑"
    if illum > 0.96:
        return "🌕"
    if 0.46 <= illum <= 0.54:
        return "🌓" if waxing else "🌗"
    if waxing:
        return "🌒" if illum < 0.5 else "🌔"
    return "🌘" if illum < 0.5 else "🌖"


def almanac(location, when_utc: dt.datetime | None = None,
            utc_offset_hours: float = 0.0, timezone: str | None = None) -> dict:
    """Tonight's sun/twilight schedule + current moon for the site."""
    if not getattr(location, "is_set", False):
        return {"available": False}
    now = when_utc or dt.datetime.now(dt.timezone.utc)
    loc = site_location(location)

    # Sun altitude over the local-noon → next-noon window (tonight's full schedule)
    local = now + dt.timedelta(hours=utc_offset_hours)
    noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
    if local.hour < 12:
        noon -= dt.timedelta(days=1)
    start = (noon - dt.timedelta(hours=utc_offset_hours)).replace(tzinfo=None)
    grid = Time(start, scale="utc") + np.arange(24 * 30 + 1) * 2 * u.min   # 2-min steps
    tu = np.asarray(grid.unix)
    sun_alt = np.asarray(get_sun(grid).transform_to(AltAz(obstime=grid, location=loc)).alt.deg)

    nowt = Time(now)
    sun_now = get_sun(nowt)
    sun_alt_now = float(sun_now.transform_to(AltAz(obstime=nowt, location=loc)).alt.deg)

    # Moon: next rise/set over the coming 24 h, current phase + position now
    mg = Time(now) + np.arange(0, 24 * 60 + 1, 4) * u.min
    mtu = np.asarray(mg.unix)
    moon_alt = np.asarray(get_body("moon", mg, loc).transform_to(AltAz(obstime=mg, location=loc)).alt.deg)

    moon_now = get_body("moon", nowt, loc)
    moon_aa = moon_now.transform_to(AltAz(obstime=nowt, location=loc))
    # phase from the geocentric Sun–Moon elongation (both geocentric GCRS → clean angle;
    # .icrs would mangle the directions, and mixed geo/topo frames warn)
    illum = (1 - math.cos(float(sun_now.separation(get_body("moon", nowt)).radian))) / 2
    later = Time(now + dt.timedelta(hours=2))
    illum_later = (1 - math.cos(float(
        get_sun(later).separation(get_body("moon", later)).radian))) / 2
    waxing = illum_later >= illum

    return {
        "available": True,
        "now_utc": now.isoformat(),
        "timezone": timezone,
        "is_dark": sun_alt_now < -18.0,
        "sun": {
            "alt_deg": round(sun_alt_now, 1),
            "sunset": _iso(_first_desc(tu, sun_alt, _SUN_HORIZON)),
            "sunrise": _iso(_first_asc(tu, sun_alt, _SUN_HORIZON)),
        },
        "twilight": {
            "civil": {"dusk": _iso(_first_desc(tu, sun_alt, -6)), "dawn": _iso(_first_asc(tu, sun_alt, -6))},
            "nautical": {"dusk": _iso(_first_desc(tu, sun_alt, -12)), "dawn": _iso(_first_asc(tu, sun_alt, -12))},
            "astronomical": {"dusk": _iso(_first_desc(tu, sun_alt, -18)), "dawn": _iso(_first_asc(tu, sun_alt, -18))},
        },
        "astronomical_night": {
            "start": _iso(_first_desc(tu, sun_alt, -18)),
            "end": _iso(_first_asc(tu, sun_alt, -18)),
        },
        "moon": {
            "illumination": round(illum, 3),
            "phase": _phase_name(illum, waxing),
            "emoji": _phase_emoji(illum, waxing),
            "waxing": waxing,
            "alt_deg": round(float(moon_aa.alt.deg), 1),
            "az_deg": round(float(moon_aa.az.deg), 1),
            "up": float(moon_aa.alt.deg) > 0,
            "rise": _iso(_first_asc(mtu, moon_alt, _MOON_HORIZON)),
            "set": _iso(_first_desc(mtu, moon_alt, _MOON_HORIZON)),
        },
    }
