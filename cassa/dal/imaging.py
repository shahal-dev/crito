"""FITS -> PNG preview generation for the operator console.

Phase 0 keeps this simple: read the primary image HDU, apply a percentile clip plus
an asinh stretch, and emit a downscaled 8-bit PNG. Full FITS handling, calibration
and plate solving arrive in Phase 2 (see docs/plan/03-DATA-PIPELINE.md).
"""
from __future__ import annotations

import io

import numpy as np
from astropy.io import fits
from PIL import Image

_PREVIEW_MAX_SIDE = 1024


def fits_to_png(fits_bytes: bytes, max_side: int = _PREVIEW_MAX_SIDE) -> bytes:
    with fits.open(io.BytesIO(fits_bytes)) as hdul:
        hdu = next((h for h in hdul if getattr(h, "data", None) is not None), hdul[0])
        data = hdu.data

    arr = np.asarray(data, dtype="float32")
    if arr.ndim == 3:  # colour cube -> first plane for a quick mono preview
        arr = arr[0]

    finite = np.isfinite(arr)
    if not finite.any():
        arr = np.zeros_like(arr)
        lo, hi = 0.0, 1.0
    else:
        lo, hi = np.percentile(arr[finite], [5.0, 99.5])
    if hi <= lo:
        hi = lo + 1.0

    scaled = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
    scaled = np.arcsinh(scaled * 10.0) / np.arcsinh(10.0)  # asinh stretch
    img8 = (scaled * 255.0).astype("uint8")

    im = Image.fromarray(img8, mode="L")
    if max(im.size) > max_side:
        ratio = max_side / max(im.size)
        im = im.resize((max(1, int(im.width * ratio)), max(1, int(im.height * ratio))))

    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()
