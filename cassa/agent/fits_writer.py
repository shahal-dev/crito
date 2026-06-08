"""FITS authoring at the edge: enrich the raw camera frame with full provenance.

The camera driver writes a minimal FITS; CASSA adds standard pointing/time/instrument
headers, a unique OBSID, FITS DATASUM/CHECKSUM and a SHA-256 so the frame is
self-describing and integrity-checkable forever. See docs/plan/03-DATA-PIPELINE.md.
"""
from __future__ import annotations

import hashlib
import io
import logging

from astropy.io import fits

log = logging.getLogger("cassa.agent")

# (header keyword, ctx key) — only written when the ctx value is not None.
_NUM_CARDS = [
    ("EXPTIME", "exptime"),
    ("RA", "ra_deg"),
    ("DEC", "dec_deg"),
    ("OBJCTALT", "alt_deg"),
    ("OBJCTAZ", "az_deg"),
    ("AIRMASS", "airmass"),
    ("FOCUSPOS", "focus_pos"),
]
_STR_CARDS = [
    ("OBSID", "obsid"),
    ("SITE", "site"),
    ("INSTRMID", "instrument_id"),
    ("DATE-OBS", "date_obs"),
    ("IMAGETYP", "image_type"),
    ("OBJECT", "object_name"),
    ("OBSERVER", "observer"),
    ("TELESCOP", "telescope"),
    ("INSTRUME", "instrument"),
    ("FILTER", "filter"),
]

_META_KEYS = (
    "obsid", "site", "ut_date", "date_obs", "exptime", "image_type", "object_name",
    "filter", "ra_deg", "dec_deg", "alt_deg", "az_deg", "airmass", "focus_pos",
    "telescope", "instrument", "observer",
)


def author_fits(raw: bytes, ctx: dict) -> dict:
    """Return {"fits": bytes, "sha256": str, "meta": dict} for an enriched frame."""
    with fits.open(io.BytesIO(raw)) as hdul:
        hdu = hdul[0]
        h = hdu.header
        for card, key in _STR_CARDS:
            val = ctx.get(key)
            if val not in (None, ""):
                h[card] = val
        for card, key in _NUM_CARDS:
            val = ctx.get(key)
            if val is not None:
                h[card] = float(val)
        h["SWCREATE"] = f"CASSA {ctx.get('version', '')}".strip()
        try:
            hdu.add_datasum()
            hdu.add_checksum()
        except Exception:
            log.debug("FITS checksum could not be computed", exc_info=True)
        width = h.get("NAXIS1")
        height = h.get("NAXIS2")
        out = io.BytesIO()
        hdul.writeto(out, overwrite=True, output_verify="silentfix")

    data = out.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    meta = {k: ctx.get(k) for k in _META_KEYS}
    meta["width"] = int(width) if width else None
    meta["height"] = int(height) if height else None
    meta["sha256"] = sha
    return {"fits": data, "sha256": sha, "meta": meta}
