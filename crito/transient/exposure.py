"""Exposure-time / SNR planner — pure math, no hardware.

Answers the operational question *"how long do I expose this target, and how many
subs?"* starting from the signal-to-noise the **science** needs. It is the CCD/CMOS
SNR ("CCD equation") plus the two practical bounds that fix the sub-exposure length
on a CMOS camera:

  * **sky-limited floor**  — a sub must be long enough that sky shot noise swamps
    read noise, otherwise stacking is inefficient:  ``t ≥ k·R² / B``.
  * **saturation ceiling** — the brightest star you care about must stay below the
    (linear) full well:  ``t ≤ frac·FW / peak_rate``.

Total integration then follows from stacking N sky-limited subs:
``SNR_total = SNR_sub · √N``  →  ``N = (SNR_required / SNR_sub)²``.

Everything is in **electrons**. The per-gain constants (read noise, full well) and
per-filter constants (sky rate, zero point) come from a calibration table measured
with :mod:`crito.calib` — but datasheet/estimated values work as a first cut, so
the planner is useful before you have characterized the camera.

Stateless and unit-testable offline, mirroring :mod:`crito.transient.visibility`.
The CMOS specifics (read noise *and* full well varying with gain) matter: see
``[[camera-touptek-g3m662m]]`` for this site's sensors.
"""
from __future__ import annotations

import math
import pathlib
from dataclasses import dataclass, field

_ARCSEC_PER_RAD = 206264.806
_FWHM_TO_SIGMA = 1.0 / 2.3548200450309493  # 1/(2*sqrt(2*ln2))


# ----------------------------------------------------------------- geometry
def pixel_scale_arcsec(pixel_size_um: float, focal_length_mm: float) -> float:
    """Plate scale in arcsec/pixel: ``206.265 · pixel_um / focal_length_mm``."""
    if focal_length_mm <= 0:
        raise ValueError("focal_length_mm must be > 0")
    return 206.264806 * pixel_size_um / focal_length_mm


def aperture_npix(fwhm_pix: float, radius_fwhm: float = 1.5) -> float:
    """Pixels inside a circular photometry aperture of radius ``radius_fwhm·FWHM``.
    1.5·FWHM encloses ~99 % of a Gaussian PSF's flux."""
    r = radius_fwhm * fwhm_pix
    return math.pi * r * r


# ----------------------------------------------------------------- photometry
def source_rate(mag: float, zero_point: float) -> float:
    """Source count rate (e-/s) for ``mag`` given the zero point (mag at 1 e-/s)."""
    return 10.0 ** (-0.4 * (mag - zero_point))


def peak_rate_per_pixel(total_rate_e_s: float, fwhm_pix: float) -> float:
    """Peak (central-pixel) count rate of a Gaussian PSF carrying ``total_rate``.
    Peak = total / (2π σ²), with σ = FWHM/2.355 in pixels."""
    sigma = fwhm_pix * _FWHM_TO_SIGMA
    return total_rate_e_s / (2.0 * math.pi * sigma * sigma)


# ----------------------------------------------------------------- SNR core
def snr_single(source_e_s: float, t: float, n_pix: float,
               sky_e_s: float, dark_e_s: float, read_noise_e: float) -> float:
    """SNR of one sub of length ``t`` (the CCD equation)."""
    signal = source_e_s * t
    var = signal + n_pix * (sky_e_s * t + dark_e_s * t + read_noise_e ** 2)
    return signal / math.sqrt(var) if var > 0 else 0.0


def subs_for_snr(required_snr: float, snr_per_sub: float) -> int:
    """How many equal subs to reach ``required_snr`` (``N = (req/sub)²``)."""
    if snr_per_sub <= 0:
        return 0
    return max(1, math.ceil((required_snr / snr_per_sub) ** 2))


def sky_limited_min_sub(sky_e_s: float, read_noise_e: float, factor: float = 10.0) -> float:
    """Shortest sub for which sky shot variance ≥ ``factor`` × read variance, i.e.
    ``t = factor·R² / B``. Below this you are paying read noise on every sub."""
    if sky_e_s <= 0:
        return 0.0
    return factor * read_noise_e ** 2 / sky_e_s


def saturation_max_sub(peak_e_s: float, full_well_e: float, sky_e_s: float,
                       dark_e_s: float, fill_frac: float = 0.7) -> float:
    """Longest sub keeping the brightest pixel below ``fill_frac`` × full well."""
    denom = peak_e_s + sky_e_s + dark_e_s
    if denom <= 0:
        return math.inf
    return fill_frac * full_well_e / denom


def mag_error(snr: float) -> float:
    """1-σ magnitude uncertainty for a given SNR: ``1.0857 / SNR``."""
    return 1.0857 / snr if snr > 0 else math.inf


# ----------------------------------------------------------------- plan
@dataclass
class ExposurePlan:
    target_mag: float
    required_snr: float
    filter: str | None
    # rates (electrons)
    source_e_per_s: float
    sky_e_per_s_per_px: float
    dark_e_per_s_per_px: float
    read_noise_e: float
    # geometry
    pixel_scale_arcsec: float
    fwhm_pix: float
    aperture_npix: float
    # sub-exposure window
    sub_min_s: float            # sky-limited floor
    sub_max_s: float            # saturation ceiling (for the brightest star)
    sub_recommended_s: float
    snr_per_sub: float
    # stack
    n_subs: int
    total_integration_s: float
    snr_achieved: float
    mag_error: float
    limiting_noise: str         # source | sky | dark | read
    warnings: list[str] = field(default_factory=list)

    def dict(self) -> dict:
        d = {
            "target_mag": self.target_mag,
            "required_snr": self.required_snr,
            "filter": self.filter,
            "source_e_per_s": round(self.source_e_per_s, 4),
            "sky_e_per_s_per_px": round(self.sky_e_per_s_per_px, 4),
            "dark_e_per_s_per_px": round(self.dark_e_per_s_per_px, 5),
            "read_noise_e": round(self.read_noise_e, 3),
            "pixel_scale_arcsec": round(self.pixel_scale_arcsec, 3),
            "fwhm_pix": round(self.fwhm_pix, 2),
            "aperture_npix": round(self.aperture_npix, 1),
            "sub_min_s": round(self.sub_min_s, 2),
            "sub_max_s": (round(self.sub_max_s, 2) if math.isfinite(self.sub_max_s) else None),
            "sub_recommended_s": round(self.sub_recommended_s, 2),
            "snr_per_sub": round(self.snr_per_sub, 2),
            "n_subs": self.n_subs,
            "total_integration_s": round(self.total_integration_s, 1),
            "total_integration_min": round(self.total_integration_s / 60.0, 1),
            "snr_achieved": round(self.snr_achieved, 1),
            "mag_error": round(self.mag_error, 4),
            "limiting_noise": self.limiting_noise,
            "warnings": self.warnings,
        }
        return d

    def summary(self) -> str:
        sub_max = (f"{self.sub_max_s:.1f}s" if math.isfinite(self.sub_max_s) else "∞")
        lines = [
            f"Target mag {self.target_mag:g}"
            + (f" ({self.filter})" if self.filter else "")
            + f"  →  required SNR {self.required_snr:g}",
            f"  source rate     {self.source_e_per_s:.3g} e-/s"
            f"   sky {self.sky_e_per_s_per_px:.3g} e-/s/px"
            f"   read noise {self.read_noise_e:.2g} e-",
            f"  plate scale     {self.pixel_scale_arcsec:.2f}\"/px"
            f"   FWHM {self.fwhm_pix:.1f} px"
            f"   aperture {self.aperture_npix:.0f} px",
            f"  sub window      sky-limited ≥ {self.sub_min_s:.1f}s,"
            f"  saturation ≤ {sub_max}",
            f"  → recommend     {self.sub_recommended_s:.1f}s subs"
            f"   (SNR/sub {self.snr_per_sub:.1f}, {self.limiting_noise}-limited)",
            f"  → {self.n_subs} subs = {self.total_integration_s / 60:.1f} min total"
            f"   → SNR {self.snr_achieved:.0f}  (±{self.mag_error:.3f} mag)",
        ]
        for w in self.warnings:
            lines.append(f"  ⚠ {w}")
        return "\n".join(lines)


def plan_exposure(
    *,
    mag: float,
    required_snr: float,
    read_noise_e: float,
    full_well_e: float,
    sky_e_per_s_per_px: float,
    zero_point: float,
    pixel_size_um: float,
    focal_length_mm: float,
    seeing_arcsec: float,
    dark_e_per_s_per_px: float = 0.0,
    brightest_mag: float | None = None,
    filter_name: str | None = None,
    aperture_radius_fwhm: float = 1.5,
    sky_factor: float = 10.0,
    saturation_fill_frac: float = 0.7,
    desired_sub_s: float | None = None,
    max_sub_s: float | None = None,
) -> ExposurePlan:
    """Plan exposure for one target. All optical/sensor constants are in electrons.

    ``brightest_mag`` is the brightest star the saturation ceiling must protect
    (defaults to the target itself — pass a bright field star for deep targets).
    ``desired_sub_s`` pins the sub length (clamped to the window); otherwise a
    sub a few× the sky-limited floor is recommended. ``max_sub_s`` caps it for
    tracking/guiding limits.
    """
    warnings: list[str] = []
    px_scale = pixel_scale_arcsec(pixel_size_um, focal_length_mm)
    fwhm_pix = seeing_arcsec / px_scale
    n_pix = aperture_npix(fwhm_pix, aperture_radius_fwhm)

    source = source_rate(mag, zero_point)
    bright = source_rate(brightest_mag if brightest_mag is not None else mag, zero_point)
    peak = peak_rate_per_pixel(bright, fwhm_pix)

    # --- sub-exposure window ------------------------------------------------
    sub_min = sky_limited_min_sub(sky_e_per_s_per_px, read_noise_e, sky_factor)
    if sky_e_per_s_per_px <= 0:
        warnings.append("no sky background supplied — sky-limited floor is undefined; "
                        "result is read-noise-limited")
    sub_max = saturation_max_sub(peak, full_well_e, sky_e_per_s_per_px,
                                 dark_e_per_s_per_px, saturation_fill_frac)
    if max_sub_s is not None:
        sub_max = min(sub_max, max_sub_s)

    # --- choose the sub length ---------------------------------------------
    if desired_sub_s is not None:
        rec = desired_sub_s
        if rec > sub_max:
            warnings.append(f"requested sub {rec:g}s saturates the brightest star "
                            f"(ceiling {sub_max:.1f}s) — clamped")
            rec = sub_max
        elif rec < sub_min:
            warnings.append(f"requested sub {rec:g}s is below the sky-limited floor "
                            f"({sub_min:.1f}s) — read-noise penalty")
    elif sub_max <= sub_min:
        rec = sub_max
        warnings.append("saturation ceiling is below the sky-limited floor: the field "
                        "saturates before sky noise dominates. Lower the gain (more "
                        "full well) or accept a small read-noise penalty.")
    else:
        rec = min(sub_max, max(sub_min, 3.0 * sub_min if sub_min > 0 else sub_max))
    rec = max(rec, 1e-3)

    # --- SNR + stack --------------------------------------------------------
    snr_sub = snr_single(source, rec, n_pix, sky_e_per_s_per_px,
                         dark_e_per_s_per_px, read_noise_e)
    n_subs = subs_for_snr(required_snr, snr_sub)
    total = n_subs * rec
    snr_total = snr_sub * math.sqrt(n_subs)

    # --- which noise term dominates at the recommended sub? -----------------
    terms = {
        "source": source * rec,
        "sky": n_pix * sky_e_per_s_per_px * rec,
        "dark": n_pix * dark_e_per_s_per_px * rec,
        "read": n_pix * read_noise_e ** 2,
    }
    limiting = max(terms, key=terms.get)

    if total > 4 * 3600:
        warnings.append(f"total integration is {total / 3600:.1f} h — consider a brighter "
                        "SNR target, a faster filter, or a bigger aperture")
    if (brightest_mag is None) and peak * rec > saturation_fill_frac * full_well_e:
        warnings.append("the target itself saturates at the recommended sub")

    return ExposurePlan(
        target_mag=mag, required_snr=required_snr, filter=filter_name,
        source_e_per_s=source, sky_e_per_s_per_px=sky_e_per_s_per_px,
        dark_e_per_s_per_px=dark_e_per_s_per_px, read_noise_e=read_noise_e,
        pixel_scale_arcsec=px_scale, fwhm_pix=fwhm_pix, aperture_npix=n_pix,
        sub_min_s=sub_min, sub_max_s=sub_max, sub_recommended_s=rec,
        snr_per_sub=snr_sub, n_subs=n_subs, total_integration_s=total,
        snr_achieved=snr_total, mag_error=mag_error(snr_total),
        limiting_noise=limiting, warnings=warnings,
    )


# ----------------------------------------------------------------- calibration
@dataclass
class Calibration:
    """A measured (or estimated) per-camera constants table, loaded from YAML.

    Layout (see ``calibration/minicam8.example.yaml``)::

        camera, sensor, pixel_size_um
        gains:   {<gain>: {read_noise_e, full_well_e, system_gain_e_per_adu}}
        dark_current_e_per_s: {<temp_c>: value}     # gain-independent (electrons)
        filters: {<name>: {sky_e_per_s_per_px, zero_point_e}}   # electrons
    """
    data: dict

    @classmethod
    def load(cls, path: str | pathlib.Path) -> "Calibration":
        import yaml
        return cls(yaml.safe_load(pathlib.Path(path).read_text()) or {})

    def _nearest(self, table: dict, key: float) -> dict:
        if not table:
            raise KeyError("calibration table section is empty")
        k = min(table, key=lambda x: abs(float(x) - key))
        return table[k]

    def gain(self, gain: float) -> dict:
        """Read noise / full well / system gain at the nearest characterized gain."""
        return self._nearest(self.data.get("gains", {}), gain)

    def filt(self, name: str) -> dict:
        """Sky rate + zero point for a filter (electrons, gain-independent)."""
        filters = self.data.get("filters", {})
        if name not in filters:
            raise KeyError(f"filter {name!r} not in calibration (have {list(filters)})")
        return filters[name]

    def dark(self, temp_c: float) -> float:
        table = self.data.get("dark_current_e_per_s", {})
        if not table:
            return 0.0
        return float(self._nearest(table, temp_c))

    def plan(self, *, mag: float, required_snr: float, filter_name: str, gain: float,
             temp_c: float, focal_length_mm: float, seeing_arcsec: float,
             pixel_size_um: float | None = None, **kw) -> ExposurePlan:
        """Build an :class:`ExposurePlan` from this table for a target."""
        g = self.gain(gain)
        f = self.filt(filter_name)
        if g.get("read_noise_e") is None or g.get("full_well_e") is None:
            raise ValueError(f"calibration for gain {gain} is incomplete "
                             "(read_noise_e / full_well_e not set — fill it in / from the datasheet)")
        return plan_exposure(
            mag=mag, required_snr=required_snr, filter_name=filter_name,
            read_noise_e=float(g["read_noise_e"]),
            full_well_e=float(g["full_well_e"]),
            sky_e_per_s_per_px=float(f["sky_e_per_s_per_px"]),
            zero_point=float(f["zero_point_e"]),
            dark_e_per_s_per_px=self.dark(temp_c),
            pixel_size_um=pixel_size_um or float(self.data.get("pixel_size_um", 0)),
            focal_length_mm=focal_length_mm, seeing_arcsec=seeing_arcsec, **kw,
        )


# ----------------------------------------------------------------- CLI
def _main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m crito.transient.exposure",
        description="Exposure-time / SNR planner. Either point at a calibration "
                    "table (--calibration + --gain + --filter) or pass the sensor "
                    "constants directly (--read-noise, --full-well, --sky, --zp).")
    p.add_argument("--mag", type=float, required=True, help="target magnitude")
    p.add_argument("--snr", type=float, required=True, help="required SNR")
    p.add_argument("--focal-length", type=float, required=True, help="mm")
    p.add_argument("--seeing", type=float, default=3.0, help="arcsec FWHM (default 3)")
    p.add_argument("--pixel-size", type=float, default=2.9, help="µm (default 2.9, IMX585)")
    p.add_argument("--brightest-mag", type=float, default=None,
                   help="brightest field star to protect from saturation")
    p.add_argument("--desired-sub", type=float, default=None, help="pin sub length (s)")
    p.add_argument("--max-sub", type=float, default=None, help="cap sub length (s)")
    # calibration-table path
    p.add_argument("--calibration", help="path to a calibration YAML")
    p.add_argument("--gain", type=float, help="gain setting (with --calibration)")
    p.add_argument("--filter", help="filter name (with --calibration)")
    p.add_argument("--temp", type=float, default=-10.0, help="sensor °C (with --calibration)")
    # manual constants
    p.add_argument("--read-noise", type=float, help="e-")
    p.add_argument("--full-well", type=float, help="e-")
    p.add_argument("--sky", type=float, help="sky e-/s/pixel")
    p.add_argument("--zp", type=float, help="zero point (mag at 1 e-/s)")
    p.add_argument("--dark", type=float, default=0.0, help="dark e-/s/pixel")
    p.add_argument("--json", action="store_true", help="emit JSON")
    a = p.parse_args(argv)

    common = dict(mag=a.mag, required_snr=a.snr, focal_length_mm=a.focal_length,
                  seeing_arcsec=a.seeing, brightest_mag=a.brightest_mag,
                  desired_sub_s=a.desired_sub, max_sub_s=a.max_sub)
    if a.calibration:
        if a.gain is None or not a.filter:
            p.error("--calibration requires --gain and --filter")
        plan = Calibration.load(a.calibration).plan(
            filter_name=a.filter, gain=a.gain, temp_c=a.temp,
            pixel_size_um=a.pixel_size, **common)
    else:
        missing = [n for n, v in [("--read-noise", a.read_noise), ("--full-well", a.full_well),
                                  ("--sky", a.sky), ("--zp", a.zp)] if v is None]
        if missing:
            p.error(f"without --calibration these are required: {', '.join(missing)}")
        plan = plan_exposure(read_noise_e=a.read_noise, full_well_e=a.full_well,
                             sky_e_per_s_per_px=a.sky, zero_point=a.zp,
                             dark_e_per_s_per_px=a.dark, pixel_size_um=a.pixel_size,
                             filter_name=a.filter, **common)

    if a.json:
        import json
        print(json.dumps(plan.dict(), indent=2))
    else:
        print(plan.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
