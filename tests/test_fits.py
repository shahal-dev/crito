"""Tests for FITS authoring (provenance headers + checksums). Requires astropy."""
import hashlib
import io
import os
import sys

import numpy as np
import pytest
from astropy.io import fits

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crito.agent.fits_writer import author_fits  # noqa: E402


def _raw_frame() -> bytes:
    data = (np.arange(32 * 48, dtype="uint16") % 1000).reshape(32, 48)
    buf = io.BytesIO()
    fits.PrimaryHDU(data).writeto(buf)
    return buf.getvalue()


def test_author_fits_headers_and_checksum():
    ctx = {
        "obsid": "virtual_20260608T120000_000001",
        "ut_date": "20260608",
        "site": "virtual",
        "instrument_id": "vinstr",
        "date_obs": "2026-06-08T12:00:00+00:00",
        "exptime": 2.0,
        "image_type": "LIGHT",
        "object_name": "M42",
        "observer": "CRITO",
        "telescope": "EQMod Mount",
        "instrument": "Toupcam",
        "version": "0.0.1",
        "ra_deg": 83.85,
        "dec_deg": -5.39,
        "alt_deg": 45.0,
        "az_deg": 120.0,
        "airmass": 1.41,
        "filter": "L",
        "focus_pos": 12000.0,
    }
    out = author_fits(_raw_frame(), ctx)

    # sha matches the returned bytes
    assert out["sha256"] == hashlib.sha256(out["fits"]).hexdigest()
    assert out["meta"]["sha256"] == out["sha256"]
    assert out["meta"]["width"] == 48 and out["meta"]["height"] == 32

    with fits.open(io.BytesIO(out["fits"])) as hdul:
        h = hdul[0].header
        assert h["OBSID"] == ctx["obsid"]
        assert h["OBJECT"] == "M42"
        assert h["IMAGETYP"] == "LIGHT"
        assert h["FILTER"] == "L"
        assert h["EXPTIME"] == pytest.approx(2.0)
        assert h["RA"] == pytest.approx(83.85)
        assert h["DEC"] == pytest.approx(-5.39)
        assert "CHECKSUM" in h and "DATASUM" in h
        assert hdul[0].data.shape == (32, 48)


def test_author_fits_omits_missing_optional_fields():
    ctx = {
        "obsid": "virtual_x",
        "ut_date": "20260608",
        "site": "virtual",
        "instrument_id": "vinstr",
        "date_obs": "2026-06-08T12:00:00+00:00",
        "exptime": 1.0,
        "image_type": "DARK",
        "version": "0.0.1",
    }
    out = author_fits(_raw_frame(), ctx)
    with fits.open(io.BytesIO(out["fits"])) as hdul:
        h = hdul[0].header
        assert "FILTER" not in h  # no filter supplied
        assert "RA" not in h      # no pointing supplied
        assert h["IMAGETYP"] == "DARK"
