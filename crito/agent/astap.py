"""ASTAP plate-solving + HFR analysis (subprocess) and its pure helpers.

ASTAP is the engine for both:
  - plate-solving (``-ra/-spd/-fov`` hints → writes a ``.ini`` with PLTSOLVD + CRVAL),
  - autofocus HFR (``-analyse`` → median HFD + star count).

The subprocess calls need ASTAP installed on the edge node; the parsing + geometry
helpers below are pure and unit-tested. ASTAP's exact ``-analyse`` output varies by
version, so the parser is tolerant (reads the .ini and stdout, multiple key spellings)
and logs the raw text — verify the keys on your install if HFR reads as null.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass

log = logging.getLogger("crito.agent.astap")


@dataclass
class SolveResult:
    ra_deg: float          # solved field-center RA (J2000/EOD per ASTAP)
    dec_deg: float         # solved field-center Dec
    scale_arcsec_px: float | None = None
    rotation_deg: float | None = None


# --------------------------------------------------------------- geometry
def compute_fov_deg(focal_length_mm: float, pixel_size_um: float, height_px: int) -> float:
    """Field-of-view height in degrees from optics + sensor height. 0 if unknown
    (→ let ASTAP auto-detect)."""
    if not focal_length_mm or not pixel_size_um or not height_px:
        return 0.0
    scale_arcsec_px = 206.265 * pixel_size_um / focal_length_mm
    return scale_arcsec_px * height_px / 3600.0


def angular_sep_arcsec(ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float) -> float:
    """Great-circle separation (arcsec) via the haversine formula."""
    r1, d1, r2, d2 = map(math.radians, (ra1_deg, dec1_deg, ra2_deg, dec2_deg))
    dr, dd = r2 - r1, d2 - d1
    a = math.sin(dd / 2) ** 2 + math.cos(d1) * math.cos(d2) * math.sin(dr / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(a)))) * 3600.0


def fit_parabola_min(xs: list[float], ys: list[float]) -> float | None:
    """Vertex x of a least-squares parabola y = a·x² + b·x + c, or None if the fit
    isn't an upward parabola (a ≤ 0) or the vertex falls outside the sampled range."""
    if len(xs) < 3:
        return None
    import numpy as np
    a, b, _c = np.polyfit(xs, ys, 2)
    if a <= 0:
        return None
    vx = -b / (2 * a)
    if vx < min(xs) or vx > max(xs):
        return None
    return float(vx)


# ----------------------------------------------------------------- parsers
def _kv(text: str) -> dict:
    """KEY=VALUE lines → upper-cased dict (ASTAP .ini + many stdout lines)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip().upper()] = v.strip()
    return out


def parse_solve_ini(text: str) -> SolveResult | None:
    """Parse an ASTAP solve .ini. Returns None unless PLTSOLVD=T."""
    d = _kv(text)
    if d.get("PLTSOLVD", "").upper() not in ("T", "TRUE", "1"):
        return None
    try:
        ra = float(d["CRVAL1"])
        dec = float(d["CRVAL2"])
    except (KeyError, ValueError):
        return None
    scale = None
    try:
        cd1 = float(d.get("CDELT1", "")) if d.get("CDELT1") else None
        if cd1:
            scale = abs(cd1) * 3600.0
    except ValueError:
        pass
    rot = None
    try:
        rot = float(d["CROTA2"]) if d.get("CROTA2") else None
    except ValueError:
        pass
    return SolveResult(ra_deg=ra, dec_deg=dec, scale_arcsec_px=scale, rotation_deg=rot)


def parse_analyse(text: str) -> tuple[float, int] | None:
    """Median HFR + star count from ASTAP -analyse output (.ini and/or stdout).
    Returns (hfr_px, star_count) or None. ASTAP reports HFD (diameter); HFR = HFD/2."""
    d = _kv(text)
    hfr = hfd = stars = None
    for k in ("HFR", "MEDIAN_HFR", "MEAN_HFR"):
        if d.get(k):
            try:
                hfr = float(d[k]); break
            except ValueError:
                pass
    for k in ("HFD", "MEDIAN_HFD", "MEAN_HFD"):
        if d.get(k):
            try:
                hfd = float(d[k]); break
            except ValueError:
                pass
    for k in ("STARS", "NSTARS", "STAR_COUNT"):
        if d.get(k):
            try:
                stars = int(float(d[k])); break
            except ValueError:
                pass
    # stdout fallbacks: "HFD=2.34", "stars detected: 152", "152 stars"
    if hfd is None and hfr is None:
        m = re.search(r"hf[dr][^\d]*([\d.]+)", text, re.I)
        if m:
            (hfd if "hfd" in m.group(0).lower() else hfr)  # noqa
            val = float(m.group(1))
            if "hfr" in m.group(0).lower():
                hfr = val
            else:
                hfd = val
    if stars is None:
        m = re.search(r"(\d+)\s*stars", text, re.I) or re.search(r"stars[^\d]*(\d+)", text, re.I)
        if m:
            stars = int(m.group(1))
    if hfr is None and hfd is not None:
        hfr = hfd / 2.0
    if hfr is None:
        return None
    return hfr, (stars or 0)


def wcs_cards(res: SolveResult, width: int, height: int) -> list[tuple[str, float, str]]:
    """Minimal TAN WCS header cards (key, value, comment) from a solve result —
    enough to register the frame; CRPIX at the image center."""
    if res.scale_arcsec_px is None:
        return []
    cd = res.scale_arcsec_px / 3600.0  # deg/px
    rot = math.radians(res.rotation_deg or 0.0)
    return [
        ("CTYPE1", "RA---TAN", "WCS projection"),
        ("CTYPE2", "DEC--TAN", "WCS projection"),
        ("CRPIX1", width / 2.0, "reference pixel"),
        ("CRPIX2", height / 2.0, "reference pixel"),
        ("CRVAL1", res.ra_deg, "RA at reference pixel (deg)"),
        ("CRVAL2", res.dec_deg, "Dec at reference pixel (deg)"),
        ("CD1_1", -cd * math.cos(rot), "WCS CD matrix"),
        ("CD1_2", cd * math.sin(rot), "WCS CD matrix"),
        ("CD2_1", cd * math.sin(rot), "WCS CD matrix"),
        ("CD2_2", cd * math.cos(rot), "WCS CD matrix"),
    ]


# --------------------------------------------------------------- subprocess
async def _run(cmd: list[str], timeout: float) -> str:
    log.debug("astap: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        raise RuntimeError(f"ASTAP binary not found: {cmd[0]!r} — install ASTAP + a star DB")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("ASTAP timed out")
    return (out or b"").decode(errors="replace")


def _ini_for(fits_path: str) -> str:
    return os.path.splitext(fits_path)[0] + ".ini"


async def solve(fits_path: str, ra_hours: float | None, dec_deg: float | None,
                fov_deg: float, s) -> SolveResult | None:
    """Plate-solve a FITS file with ASTAP. ra/dec hints speed it up enormously."""
    cmd = [s.astap_path, "-f", fits_path, "-r", f"{s.astap_search_radius_deg:g}"]
    if fov_deg and fov_deg > 0:
        cmd += ["-fov", f"{fov_deg:.4f}"]
    if ra_hours is not None and dec_deg is not None:
        cmd += ["-ra", f"{ra_hours:.6f}", "-spd", f"{dec_deg + 90:.6f}"]
    if s.astap_downsample:
        cmd += ["-z", str(s.astap_downsample)]
    if s.solve_db:
        cmd += ["-d", s.solve_db]
    cmd += ["-wcs"]
    stdout = await _run(cmd, timeout=s.solve_exposure_s + 60.0)
    ini = _ini_for(fits_path)
    text = stdout
    if os.path.exists(ini):
        try:
            text += "\n" + open(ini).read()
        except OSError:
            pass
    res = parse_solve_ini(text)
    if res is None:
        log.warning("ASTAP solve failed; output:\n%s", text[:800])
    return res


async def analyse(fits_path: str, s) -> tuple[float, int] | None:
    """Measure median HFR + star count with ASTAP -analyse (no solve)."""
    cmd = [s.astap_path, "-f", fits_path, "-analyse", f"{s.af_min_snr:g}"]
    if s.solve_db:
        cmd += ["-d", s.solve_db]
    stdout = await _run(cmd, timeout=60.0)
    ini = _ini_for(fits_path)
    text = stdout
    if os.path.exists(ini):
        try:
            text += "\n" + open(ini).read()
        except OSError:
            pass
    res = parse_analyse(text)
    if res is None:
        log.debug("ASTAP analyse: no HFR parsed; output:\n%s", text[:500])
    return res


class _TempFits:
    """Write raw FITS bytes to a temp file ASTAP can read; clean up the dir + sidecars."""
    def __init__(self, raw: bytes):
        self.raw = raw
        self.dir: str | None = None
        self.path: str | None = None

    def __enter__(self) -> str:
        self.dir = tempfile.mkdtemp(prefix="crito-solve-")
        self.path = os.path.join(self.dir, "frame.fits")
        with open(self.path, "wb") as f:
            f.write(self.raw)
        return self.path

    def __exit__(self, *exc) -> None:
        if self.dir:
            shutil.rmtree(self.dir, ignore_errors=True)
