"""Tests for the local OpenNGC object-name catalog (offline name → RA/Dec)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crito.core.catalog import get_catalog, normalize  # noqa: E402


def test_normalize_identifiers():
    assert normalize("M 42") == "m42"
    assert normalize("M42") == "m42"
    assert normalize("Messier 42") == "m42"
    assert normalize("NGC 1976") == "ngc1976"
    assert normalize("NGC0224") == "ngc224"        # leading zeros stripped
    assert normalize("Orion Nebula") == "orionnebula"
    assert normalize("Vega") == "vega"             # non-id → alphanumerics


def test_catalog_lookup_messier_variants():
    cat = get_catalog()
    assert cat.size > 10000
    m42 = cat.lookup("M42")
    assert m42 is not None
    assert abs(m42["ra_deg"] - 83.82) < 0.2 and abs(m42["dec_deg"] + 5.39) < 0.2
    assert abs(m42["ra_hours"] - m42["ra_deg"] / 15.0) < 1e-9
    # spacing / case / catalog id all resolve to the same object
    assert cat.lookup("m 42")["ra_deg"] == m42["ra_deg"]
    assert cat.lookup("NGC 1976")["ra_deg"] == m42["ra_deg"]


def test_catalog_lookup_common_name():
    cat = get_catalog()
    andro = cat.lookup("Andromeda Galaxy")
    assert andro is not None and abs(andro["ra_deg"] - 10.68) < 0.2
    assert cat.lookup("M31")["ra_deg"] == andro["ra_deg"]


def test_catalog_miss_returns_none():
    assert get_catalog().lookup("definitely not a catalogued object zzz") is None
