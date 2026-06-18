"""Tests for ASTAP parsing + plate-solve/autofocus geometry (offline, no ASTAP binary)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cassa.agent import astap  # noqa: E402


def test_compute_fov():
    # 600 mm fl, 2.9 µm pixels, 1080 px tall → ~0.30°
    assert round(astap.compute_fov_deg(600, 2.9, 1080), 3) == 0.299
    assert astap.compute_fov_deg(0, 2.9, 1080) == 0.0      # unknown fl → auto
    assert astap.compute_fov_deg(600, 0, 1080) == 0.0


def test_angular_sep():
    assert round(astap.angular_sep_arcsec(83.8, -5.0, 83.8, -4.0), 0) == 3600.0   # 1° in dec
    assert astap.angular_sep_arcsec(10.0, 20.0, 10.0, 20.0) == 0.0
    # 1 arcsec in RA at the equator
    assert round(astap.angular_sep_arcsec(10.0, 0.0, 10.0 + 1 / 3600.0, 0.0), 2) == 1.0


def test_parse_solve_ini():
    r = astap.parse_solve_ini("PLTSOLVD=T\nCRVAL1=83.8221\nCRVAL2=-5.3911\nCDELT1=-0.000275\nCROTA2=1.5\n")
    assert r is not None
    assert round(r.ra_deg, 4) == 83.8221 and round(r.dec_deg, 4) == -5.3911
    assert round(r.scale_arcsec_px, 3) == 0.99
    assert r.rotation_deg == 1.5


def test_parse_solve_ini_failure():
    assert astap.parse_solve_ini("PLTSOLVD=F\nERROR=no stars detected") is None
    assert astap.parse_solve_ini("garbage\nno keys") is None


def test_parse_analyse():
    assert astap.parse_analyse("HFD=4.20\nSTARS=152\n") == (2.1, 152)       # HFR = HFD/2
    assert astap.parse_analyse("HFR=2.5\nSTARS=10") == (2.5, 10)
    hfr, n = astap.parse_analyse("Median HFD=3.1, 88 stars detected")        # stdout style
    assert hfr == 1.55 and n == 88
    assert astap.parse_analyse("no stars here") is None


def test_fit_parabola_min():
    # symmetric V around 300
    assert astap.fit_parabola_min([100, 200, 300, 400, 500], [5, 3, 2, 3, 5]) == 300.0
    # not an upward parabola → None
    assert astap.fit_parabola_min([1, 2, 3], [3, 2, 1]) is None
    # too few points
    assert astap.fit_parabola_min([1, 2], [1, 2]) is None
    # minimum outside the sampled range → None (don't extrapolate)
    assert astap.fit_parabola_min([100, 200, 300], [2, 3, 5]) is None


def test_wcs_cards():
    r = astap.SolveResult(ra_deg=83.8, dec_deg=-5.39, scale_arcsec_px=0.99, rotation_deg=0.0)
    cards = dict((c[0], c[1]) for c in astap.wcs_cards(r, 1920, 1080))
    assert cards["CTYPE1"] == "RA---TAN" and cards["CRVAL1"] == 83.8
    assert cards["CRPIX1"] == 960.0 and cards["CRPIX2"] == 540.0
    assert astap.wcs_cards(astap.SolveResult(83.8, -5.39), 1920, 1080) == []   # no scale → no WCS
