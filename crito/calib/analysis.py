"""Sensor characterization math — pure numpy/astropy, no hardware.

Turns sets of calibration frames into the physical constants the exposure planner
needs:

  * read noise (e-)              — from a pair of bias frames
  * system gain (e-/ADU)         — photon transfer on a flat pair + a bias pair
  * dark current (e-/s/pixel)    — from darks at the operating temperature
  * sky background (e-/s/pixel)  — from an on-sky frame, per filter
  * photometric zero point       — from one star of known magnitude

On a CMOS sensor read noise and full well change with the *gain setting*, so every
measurement is tied to the gain it was taken at. The math works on the two-frame
*difference* wherever possible: subtracting two nominally identical frames cancels
the fixed-pattern (spatial) noise and leaves twice the temporal (shot + read)
variance we actually want — ``var(a-b) = 2·var_temporal``.

This module is stateless and unit-testable offline: hand it numpy arrays (real or
synthetic) and it returns the constants — no camera required. The hardware side
that actually takes the frames lives in :mod:`crito.calib.characterize`.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass

import numpy as np
from astropy.io import fits
from astropy.stats import sigma_clipped_stats


# --------------------------------------------------------------------- frame I/O
def load_fits_array(raw: bytes | str) -> np.ndarray:
    """Primary-HDU image as float64. Accepts raw FITS bytes or a file path."""
    src = io.BytesIO(raw) if isinstance(raw, (bytes, bytearray)) else raw
    with fits.open(src) as hdul:
        data = next((h.data for h in hdul if h.data is not None), None)
    if data is None:
        raise ValueError("no image data in FITS")
    return np.asarray(data, dtype=np.float64)


def _center(arr: np.ndarray, frac: float = 0.5) -> np.ndarray:
    """Central ``frac`` box — avoids vignetted edges and amp-glow corners."""
    a = np.squeeze(arr)
    h, w = a.shape[-2:]
    dh, dw = int(h * (1 - frac) / 2), int(w * (1 - frac) / 2)
    return a[dh:h - dh, dw:w - dw]


# ----------------------------------------------------------------- basic stats
def frame_level(arr: np.ndarray, frac: float = 0.5) -> float:
    """Sigma-clipped mean (ADU) of the central box."""
    mean, _, _ = sigma_clipped_stats(_center(arr, frac))
    return float(mean)


def diff_variance(a: np.ndarray, b: np.ndarray, frac: float = 0.5) -> float:
    """Per-pixel *temporal* variance (ADU²) from a frame pair.

    ``var(a-b) = 2·var_temporal`` because the two frames are independent and their
    identical fixed-pattern component cancels in the difference, so we halve it.
    """
    d = _center(a, frac) - _center(b, frac)
    _, _, std = sigma_clipped_stats(d)
    return float(std) ** 2 / 2.0


def read_noise_adu(bias1: np.ndarray, bias2: np.ndarray, frac: float = 0.5) -> float:
    """Read noise in ADU from a bias pair: ``std(b1-b2)/√2``."""
    return math.sqrt(diff_variance(bias1, bias2, frac))


# ------------------------------------------------------------- gain + read noise
@dataclass
class GainResult:
    gain_e_per_adu: float    # system gain (electrons per ADU)
    read_noise_e: float      # read noise (electrons)
    read_noise_adu: float
    signal_adu: float        # mean flat level used, bias-subtracted
    bias_adu: float

    def dict(self) -> dict:
        return {
            "gain_e_per_adu": round(self.gain_e_per_adu, 5),
            "read_noise_e": round(self.read_noise_e, 3),
            "read_noise_adu": round(self.read_noise_adu, 3),
            "signal_adu": round(self.signal_adu, 1),
            "bias_adu": round(self.bias_adu, 1),
        }


def gain_read_noise(flat1, flat2, bias1, bias2, frac: float = 0.5) -> GainResult:
    """System gain + read noise from one flat pair and one bias pair (Janesick
    two-image photon transfer).

        g [e-/ADU] = signal_adu / ( var_temporal(flats) - var_temporal(bias) )

    The denominator is the pure shot variance in ADU² (read + fixed-pattern noise
    removed by the differencing), and shot variance in electrons equals the signal
    in electrons, so the ratio is electrons-per-ADU. Read noise (e-) is then the
    bias read noise scaled by that gain. Flats should sit ~30–60 % of full well —
    bright enough to be shot-dominated, well inside the linear range.
    """
    bias = 0.5 * (frame_level(bias1, frac) + frame_level(bias2, frac))
    signal = 0.5 * (frame_level(flat1, frac) + frame_level(flat2, frac)) - bias
    shot = diff_variance(flat1, flat2, frac) - diff_variance(bias1, bias2, frac)
    if shot <= 0:
        raise ValueError("flat shot variance ≤ 0 — flats too dim, saturated, or not a flat?")
    g = signal / shot
    rn_adu = read_noise_adu(bias1, bias2, frac)
    return GainResult(gain_e_per_adu=g, read_noise_e=rn_adu * g,
                      read_noise_adu=rn_adu, signal_adu=signal, bias_adu=bias)


def gain_from_ptc(signals_adu, variances_adu) -> tuple[float, float]:
    """Fit a photon-transfer curve (variance vs signal, both ADU) over several
    illumination levels and return ``(gain_e_per_adu, read_noise_e)``.

    ``var = read_var + signal/gain`` → slope = 1/gain, intercept = read variance.
    More robust than a single pair when you can ramp the flat level.
    """
    x = np.asarray(signals_adu, float)
    y = np.asarray(variances_adu, float)
    if x.size < 2:
        raise ValueError("need ≥2 illumination levels for a PTC fit")
    slope, intercept = np.polyfit(x, y, 1)
    if slope <= 0:
        raise ValueError("non-positive PTC slope — check the frames")
    gain = 1.0 / slope
    read_var = max(intercept, 0.0)
    return gain, math.sqrt(read_var) * gain


# ----------------------------------------------------------------- dark current
def dark_current(dark, bias_adu: float, gain_e_per_adu: float, exptime_s: float,
                 frac: float = 0.5) -> float:
    """Dark current (e-/s/pixel) from one dark: ``(mean_dark - bias)·gain / t``."""
    if exptime_s <= 0:
        raise ValueError("dark exposure time must be > 0")
    level = frame_level(dark, frac) - bias_adu
    return max(level, 0.0) * gain_e_per_adu / exptime_s


def dark_current_series(darks, exptimes_s, bias_adu: float, gain_e_per_adu: float,
                        frac: float = 0.5) -> float:
    """Dark current (e-/s/pixel) as the slope of (signal vs exposure) over several
    dark exposure times — the line's slope removes any residual bias offset."""
    t = np.asarray(exptimes_s, float)
    lvl = np.array([frame_level(d, frac) - bias_adu for d in darks], float)
    if t.size < 2:
        return dark_current(darks[0], bias_adu, gain_e_per_adu, float(t[0]), frac)
    slope = np.polyfit(t, lvl, 1)[0]
    return max(float(slope), 0.0) * gain_e_per_adu


# -------------------------------------------------------------------- sky + ZP
def sky_rate(light, bias_adu: float, dark_e_per_s: float, gain_e_per_adu: float,
             exptime_s: float, frac: float = 0.5) -> float:
    """Sky background (e-/s/pixel) from an on-sky frame.

    Uses the *median* of the central box so stars (a small bright minority) are
    rejected, then converts to electrons and subtracts the dark contribution.
    """
    if exptime_s <= 0:
        raise ValueError("sky exposure time must be > 0")
    med = float(np.median(_center(light, frac))) - bias_adu
    return max(med * gain_e_per_adu / exptime_s - dark_e_per_s, 0.0)


def zero_point(flux_e_per_s: float, catalog_mag: float) -> float:
    """Photometric zero point — the instrumental magnitude that yields 1 e-/s:

        ZP = catalog_mag + 2.5·log10(flux_e_per_s)

    Inverse (used by the planner): ``flux = 10**(-0.4·(mag - ZP))``.
    """
    if flux_e_per_s <= 0:
        raise ValueError("flux must be > 0")
    return catalog_mag + 2.5 * math.log10(flux_e_per_s)


def full_well_from_saturation(saturation_adu: float, bias_adu: float,
                              gain_e_per_adu: float) -> float:
    """Approximate full well (e-) from the ADU at which the sensor saturates:
    ``(sat - bias)·gain``. A rough cross-check on the datasheet value — the true
    *linear* full well is somewhat below hard saturation."""
    return max(saturation_adu - bias_adu, 0.0) * gain_e_per_adu
