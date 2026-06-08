"""Archive service: ingest an authored FITS, store bytes + previews, index metadata.

In Phase 1 this runs in-process; the same interface is the seam where, in a
multi-site deployment, frames arrive from edge nodes over SFTP before ingest.
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import desc, select

from ..dal.imaging import fits_to_png
from .db import Image
from .storage import LocalStore

log = logging.getLogger("cassa.archive")

_THUMB_SIDE = 256
_PREVIEW_SIDE = 1024


class ArchiveService:
    def __init__(self, store: LocalStore, sessionmaker):
        self.store = store
        self.sm = sessionmaker

    async def ingest(self, fits_bytes: bytes, meta: dict) -> dict:
        sha = hashlib.sha256(fits_bytes).hexdigest()
        if meta.get("sha256") and meta["sha256"] != sha:
            raise ValueError("checksum mismatch on ingest")

        ut = meta["ut_date"]
        obsid = meta["obsid"]
        raw_key = f"raw/{ut}/{obsid}.fits"
        self.store.put(raw_key, fits_bytes)

        preview_key = thumb_key = ""
        try:
            self.store.put(f"previews/{ut}/{obsid}.png", fits_to_png(fits_bytes, _PREVIEW_SIDE))
            self.store.put(f"thumbs/{ut}/{obsid}.png", fits_to_png(fits_bytes, _THUMB_SIDE))
            preview_key = f"previews/{ut}/{obsid}.png"
            thumb_key = f"thumbs/{ut}/{obsid}.png"
        except Exception:
            log.exception("preview generation failed for %s", obsid)

        img = Image(
            id=obsid,
            obsid=obsid,
            ut_date=ut,
            date_obs=meta["date_obs"],
            exptime=float(meta["exptime"]),
            image_type=meta.get("image_type", "LIGHT"),
            object_name=meta.get("object_name") or "",
            filter=meta.get("filter"),
            ra_deg=meta.get("ra_deg"),
            dec_deg=meta.get("dec_deg"),
            alt_deg=meta.get("alt_deg"),
            az_deg=meta.get("az_deg"),
            airmass=meta.get("airmass"),
            focus_pos=meta.get("focus_pos"),
            width=meta.get("width"),
            height=meta.get("height"),
            telescope=meta.get("telescope"),
            instrument=meta.get("instrument"),
            observer=meta.get("observer"),
            sha256=sha,
            fits_key=raw_key,
            preview_key=preview_key,
            thumb_key=thumb_key,
        )
        async with self.sm() as session:
            session.add(img)
            await session.commit()
            result = img.dict()
        log.info("archived %s (%s, %.1fs)", obsid, img.image_type, img.exptime)
        return result

    async def list_images(self, limit: int = 50) -> list[dict]:
        async with self.sm() as session:
            res = await session.execute(
                select(Image).order_by(desc(Image.created_at)).limit(limit)
            )
            return [row.dict() for row in res.scalars().all()]

    async def get(self, image_id: str) -> Image | None:
        async with self.sm() as session:
            return await session.get(Image, image_id)
