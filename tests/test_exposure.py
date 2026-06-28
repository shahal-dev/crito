"""Tests for the exposure/SNR planner — pure math, offline, no hardware."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from crito.transient.exposure import (  # noqa: E402
    Calibration,
    aperture_npix,
    mag_error,
    pixel_scale_arcsec,
    plan_exposure,
    sky_limited_min_sub,
    snr_single,
    source_rate,
    subs_for_snr,
)


# ----------------------------------------------------------------- geometry
def test_pixel_scale_known_value():
    # 2.9 µm pixels at 1000 mm: 206.265 * 2.9 / 1000 ≈ 0.598"/px
    assert pixel_scale_arcsec(2.9, 1000.0) == pytest.approx(0.598, abs=1e-3)


def test_pixel_scale_rejects_zero_focal_length():
    with pytest.raises(ValueError):
        pixel_scale_arcsec(2.9, 0.0)


def test_aperture_npix_scales_with_radius():
    n1 = aperture_npix(4.0, radius_fwhm=1.0)
    n2 = aperture_npix(4.0, radius_fwhm=2.0)
    assert n2 == pytest.approx(4 * n1)  # area ∝ radius²


# ----------------------------------------------------------------- photometry
def test_source_rate_zero_point_definition():
    # at mag == zero point the rate is exactly 1 e-/s
    assert source_rate(20.0, 20.0) == pytest.approx(1.0)
    # 5 magnitudes brighter == ×100 flux
    assert source_rate(15.0, 20.0) == pytest.approx(100.0)


# ----------------------------------------------------------------- SNR core
def test_snr_root_n_scaling():
    # in any regime, equal subs stack as sqrt(N)
    s = snr_single(50.0, 10.0, 20.0, 5.0, 0.01, 2.0)
    assert subs_for_snr(s * 3.0, s) == 9            # need 9 subs for 3× the SNR
    assert subs_for_snr(s, s) == 1


def test_snr_pure_source_limited_is_sqrt_signal():
    # no sky/dark/read → SNR = sqrt(signal)
    s = snr_single(100.0, 9.0, 1.0, 0.0, 0.0, 0.0)
    assert s == pytest.approx(math.sqrt(900.0))


def test_sky_limited_floor_formula():
    # t where B*t = factor*R²  →  10 * 1² / 5 = 2 s
    assert sky_limited_min_sub(5.0, 1.0, factor=10.0) == pytest.approx(2.0)
    # no sky → no floor
    assert sky_limited_min_sub(0.0, 1.0) == 0.0


def test_mag_error():
    assert mag_error(100.0) == pytest.approx(0.010857, abs=1e-6)


# ----------------------------------------------------------------- plan_exposure
def _base(**kw):
    args = dict(
        mag=16.0, required_snr=100.0, read_noise_e=1.1, full_well_e=13000.0,
        sky_e_per_s_per_px=8.0, zero_point=21.0, pixel_size_um=2.9,
        focal_length_mm=500.0, seeing_arcsec=3.0, dark_e_per_s_per_px=0.01,
    )
    args.update(kw)
    return plan_exposure(**args)


def test_plan_reaches_required_snr():
    p = _base()
    assert p.snr_achieved >= p.required_snr        # stack must meet the target
    assert p.n_subs >= 1
    assert p.total_integration_s == pytest.approx(p.n_subs * p.sub_recommended_s)


def test_plan_bright_urban_is_sky_limited_short_subs():
    # bright broadband sky + low read noise → sky-limited, short floor
    p = _base(mag=14.0, sky_e_per_s_per_px=20.0, read_noise_e=1.0)
    assert p.limiting_noise in ("sky", "source")
    assert p.sub_min_s < 5.0                        # sky-limited within seconds


def test_plan_narrowband_needs_longer_subs_than_broadband():
    broad = _base(filter_name="L", sky_e_per_s_per_px=8.0)
    narrow = _base(filter_name="Ha", sky_e_per_s_per_px=0.12)
    # darker sky → the sky-limited floor moves to much longer subs
    assert narrow.sub_min_s > broad.sub_min_s * 10


def test_plan_saturation_ceiling_caps_bright_star():
    # a bright field star forces a short saturation ceiling
    p = _base(mag=18.0, brightest_mag=8.0, full_well_e=13000.0)
    assert math.isfinite(p.sub_max_s)
    assert p.sub_recommended_s <= p.sub_max_s + 1e-9


def test_plan_warns_when_saturation_below_sky_floor():
    # very bright star + tiny full well + dark narrowband sky → conflict
    p = _base(mag=20.0, brightest_mag=6.0, sky_e_per_s_per_px=0.05,
              full_well_e=9000.0, read_noise_e=1.0)
    assert p.sub_max_s < p.sub_min_s
    assert any("saturation" in w.lower() for w in p.warnings)


def test_plan_desired_sub_is_clamped_to_ceiling():
    p = _base(mag=12.0, brightest_mag=10.0, full_well_e=13000.0, desired_sub_s=600.0)
    assert p.sub_recommended_s <= p.sub_max_s + 1e-9
    assert any("clamp" in w.lower() or "saturat" in w.lower() for w in p.warnings)


def test_fainter_target_needs_more_total_time():
    bright = _base(mag=14.0)
    faint = _base(mag=18.0)
    assert faint.total_integration_s > bright.total_integration_s


# ----------------------------------------------------------------- Calibration
def test_calibration_lookup_and_plan(tmp_path):
    import textwrap
    path = tmp_path / "cal.yaml"
    path.write_text(textwrap.dedent("""
        camera: test
        pixel_size_um: 2.9
        gains:
          "0":   { read_noise_e: 5.0, full_well_e: 51000, system_gain_e_per_adu: 0.8 }
          "120": { read_noise_e: 1.1, full_well_e: 13000, system_gain_e_per_adu: 0.16 }
        dark_current_e_per_s:
          "-10": 0.01
        filters:
          L:  { sky_e_per_s_per_px: 8.0,  zero_point_e: 21.0 }
          Ha: { sky_e_per_s_per_px: 0.12, zero_point_e: 18.6 }
    """))
    cal = Calibration.load(path)
    # nearest-gain lookup
    assert cal.gain(110)["read_noise_e"] == 1.1
    assert cal.gain(10)["read_noise_e"] == 5.0
    assert cal.dark(-9) == 0.01
    p = cal.plan(mag=17.0, required_snr=50.0, filter_name="Ha", gain=120, temp_c=-10,
                 focal_length_mm=500.0, seeing_arcsec=3.0)
    assert p.read_noise_e == 1.1
    assert p.snr_achieved >= 50.0


def test_calibration_unknown_filter_raises(tmp_path):
    path = tmp_path / "cal.yaml"
    path.write_text('filters: {L: {sky_e_per_s_per_px: 8.0, zero_point_e: 21.0}}\n')
    with pytest.raises(KeyError):
        Calibration.load(path).filt("ZZ")


# ----------------------------------------------------------------- setups
def test_setups_view_synthesized_from_equipment():
    from crito.core.observatory import Observatory
    obs = Observatory(**{"equipment": {
        "telescope": {"name": "200P", "focal_length_mm": 1000},
        "cameras": [{"role": "camera", "name": "miniCAM8", "pixel_size_um": 2.9}]}})
    v = obs.setups_view()
    assert len(v) == 1
    assert v[0]["focal_length_mm"] == 1000 and v[0]["pixel_size_um"] == 2.9
    assert "200P" in v[0]["name"] and "miniCAM8" in v[0]["name"]


def test_setups_view_declared_pass_through():
    from crito.core.observatory import Observatory
    obs = Observatory(**{"setups": [
        {"id": "reducer", "name": "200P + 0.5x", "focal_length_mm": 500,
         "pixel_size_um": 2.9, "calibration_file": "calibration/x.yaml"}]})
    v = obs.setups_view()
    assert v[0]["id"] == "reducer" and v[0]["focal_length_mm"] == 500
    assert v[0]["calibration_file"] == "calibration/x.yaml"
