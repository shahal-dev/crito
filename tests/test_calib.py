"""Tests for sensor-characterization math — synthetic frames, offline, no hardware.

Synthetic frames model the real signal chain: electrons ~ Poisson, converted to
ADU by the system gain, plus Gaussian read noise and a bias pedestal. The analysis
must recover the gain / read noise / dark current we put in.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from crito.calib.analysis import (  # noqa: E402
    dark_current,
    dark_current_series,
    frame_level,
    gain_from_ptc,
    gain_read_noise,
    read_noise_adu,
    sky_rate,
    zero_point,
)

RNG = np.random.default_rng(42)
SHAPE = (320, 320)


def _bias(bias_adu, rn_adu, rng=RNG):
    return bias_adu + rng.normal(0.0, rn_adu, SHAPE)


def _flat(signal_e, gain_e_per_adu, bias_adu, rn_adu, rng=RNG):
    """A flat at `signal_e` electrons/pixel, rendered to ADU through the chain."""
    electrons = rng.poisson(signal_e, SHAPE).astype(float)
    return bias_adu + electrons / gain_e_per_adu + rng.normal(0.0, rn_adu, SHAPE)


# ----------------------------------------------------------------- read noise
def test_read_noise_adu_recovered():
    b1, b2 = _bias(500.0, 2.5), _bias(500.0, 2.5)
    assert read_noise_adu(b1, b2) == pytest.approx(2.5, rel=0.05)


def test_frame_level_recovered():
    assert frame_level(_bias(500.0, 2.5)) == pytest.approx(500.0, abs=0.1)


# ----------------------------------------------------------------- gain (PTC)
def test_gain_read_noise_two_image():
    g, rn_adu, bias, sig_e = 0.8, 2.5, 500.0, 8000.0
    f1 = _flat(sig_e, g, bias, rn_adu)
    f2 = _flat(sig_e, g, bias, rn_adu)
    b1, b2 = _bias(bias, rn_adu), _bias(bias, rn_adu)
    res = gain_read_noise(f1, f2, b1, b2)
    assert res.gain_e_per_adu == pytest.approx(g, rel=0.05)
    assert res.read_noise_e == pytest.approx(rn_adu * g, rel=0.08)
    assert res.signal_adu == pytest.approx(sig_e / g, rel=0.02)


def test_gain_read_noise_rejects_non_flat():
    # identical "flats" → zero shot variance, below the bias variance → must raise
    b = _bias(500.0, 2.5)
    with pytest.raises(ValueError):
        gain_read_noise(b, b, _bias(500.0, 2.5), _bias(500.0, 2.5))


def test_gain_from_ptc_fit():
    g, rn_adu, bias = 0.8, 2.5, 500.0
    signals, variances = [], []
    for sig_e in (2000.0, 5000.0, 10000.0, 20000.0):
        f1, f2 = _flat(sig_e, g, bias, rn_adu), _flat(sig_e, g, bias, rn_adu)
        b1, b2 = _bias(bias, rn_adu), _bias(bias, rn_adu)
        signals.append(0.5 * (frame_level(f1) + frame_level(f2)) - bias)
        # var(F1-F2)/2 is the per-frame temporal variance the PTC fit expects
        from crito.calib.analysis import diff_variance
        variances.append(diff_variance(f1, f2))
    gain, rn_e = gain_from_ptc(signals, variances)
    assert gain == pytest.approx(g, rel=0.05)        # slope → gain is reliable
    # the read-noise intercept (~6 ADU²) is dwarfed by signal variance (~30000 ADU²),
    # so the PTC read noise is only a sanity check — measure it from bias pairs instead.
    assert rn_e >= 0.0 and rn_e < 5.0 * rn_adu * g


# ----------------------------------------------------------------- dark current
def test_dark_current_single():
    g, bias, D, t = 0.8, 500.0, 0.05, 200.0  # 0.05 e-/s * 200 s = 10 e-
    dark = _flat(D * t, g, bias, 2.5)         # dark "signal" is the accumulated charge
    assert dark_current(dark, bias, g, t) == pytest.approx(D, rel=0.1)


def test_dark_current_series_slope():
    g, bias, D = 0.8, 500.0, 0.05
    ts = [50.0, 100.0, 200.0, 400.0]
    darks = [_flat(D * t, g, bias, 2.5) for t in ts]
    assert dark_current_series(darks, ts, bias, g) == pytest.approx(D, rel=0.1)


# ----------------------------------------------------------------- sky + ZP
def test_sky_rate_median_rejects_dark_signal():
    g, bias, sky_e_s, t = 0.8, 500.0, 4.0, 30.0    # 4 e-/s * 30 s = 120 e- sky
    light = _flat(sky_e_s * t, g, bias, 2.5)
    assert sky_rate(light, bias, 0.0, g, t) == pytest.approx(sky_e_s, rel=0.05)


def test_zero_point_roundtrip():
    # ZP such that a mag-15 star gives 1000 e-/s
    zp = zero_point(1000.0, 15.0)
    flux = 10.0 ** (-0.4 * (15.0 - zp))
    assert flux == pytest.approx(1000.0, rel=1e-6)
