"""Device Manager: holds the INDI connection and the site's devices.

Keeps a resilient background connection to ``indiserver``: if the server is not yet
up (or drops), it retries. Mount + camera are required; focuser + filter wheel are
optional (tolerated if a site lacks them). ``capture()`` orchestrates a full manual
exposure and authors a provenance-rich FITS frame.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone

from .. import __version__
from ..dal.indi.protocol import INDIClient
from ..dal.indi_adapter import IndiCamera, IndiFilterWheel, IndiFocuser, IndiMount
from .fits_writer import author_fits

log = logging.getLogger("cassa.agent")

_RECONNECT_DELAY = 2.0


class DeviceManager:
    def __init__(self, settings):
        self.settings = settings
        self.client = INDIClient(settings.indi_host, settings.indi_port)
        self.mount: IndiMount | None = None
        self.camera: IndiCamera | None = None
        self.focuser: IndiFocuser | None = None
        self.filterwheel: IndiFilterWheel | None = None
        self.connected = False
        self.latest_png: bytes | None = None
        self.latest_image_at: str | None = None
        self._stop = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.client.close()

    async def _run(self) -> None:
        while not self._stop:
            try:
                await self.client.connect()
                await self._setup()
                self.connected = True
                log.info("site online (mount=%s, camera=%s)",
                         self.settings.mount_device, self.settings.camera_device)
                await self.client.wait_closed()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("INDI connection problem: %s", e)
            finally:
                self.connected = False
                self.mount = self.camera = self.focuser = self.filterwheel = None
            if self._stop:
                break
            await asyncio.sleep(_RECONNECT_DELAY)

    async def _setup(self) -> None:
        s = self.settings
        self.mount = IndiMount(self.client, s.mount_device)
        self.camera = IndiCamera(self.client, s.camera_device, on_image=self._on_image)
        await self.mount.connect()
        await self.camera.connect()
        # optional devices — a site may not have them
        try:
            f = IndiFocuser(self.client, s.focuser_device)
            await f.connect(timeout=8.0)
            self.focuser = f
        except Exception as e:
            log.info("no focuser (%s): %s", s.focuser_device, e)
        try:
            w = IndiFilterWheel(self.client, s.filterwheel_device)
            await w.connect(timeout=8.0)
            self.filterwheel = w
        except Exception as e:
            log.info("no filter wheel (%s): %s", s.filterwheel_device, e)

    def _on_image(self, png: bytes) -> None:
        self.latest_png = png
        self.latest_image_at = datetime.now(timezone.utc).isoformat()

    async def capture(self, seconds: float, image_type: str = "LIGHT",
                      object_name: str = "", filter_slot: int | None = None) -> dict:
        """Run a full manual exposure and return an authored FITS + metadata."""
        if not (self.connected and self.mount and self.camera):
            raise RuntimeError("devices not connected")

        if filter_slot and self.filterwheel:
            await self.filterwheel.set_position(int(filter_slot))
            await self.client.wait_for(
                lambda: self.filterwheel.status().position == int(filter_slot)
                and not self.filterwheel.status().moving,
                timeout=20.0,
            )

        now = datetime.now(timezone.utc)
        obsid = f"{self.settings.site_id}_{now:%Y%m%dT%H%M%S_%f}"
        mst = self.mount.status()
        wst = self.filterwheel.status() if self.filterwheel else None
        fst = self.focuser.status() if self.focuser else None

        ra_deg = mst.ra_hours * 15.0 if mst.ra_hours is not None else None
        alt = mst.alt_deg
        airmass = 1.0 / math.sin(math.radians(alt)) if (alt and alt > 3.0) else None

        raw = await self.camera.capture(seconds)
        ctx = {
            "obsid": obsid,
            "ut_date": now.strftime("%Y%m%d"),
            "site": self.settings.site_id,
            "instrument_id": self.settings.instrument_id,
            "date_obs": now.isoformat(),
            "exptime": seconds,
            "image_type": image_type,
            "object_name": object_name,
            "observer": self.settings.observer,
            "telescope": self.settings.telescope_name,
            "instrument": self.settings.instrument_name,
            "version": __version__,
            "ra_deg": ra_deg,
            "dec_deg": mst.dec_deg,
            "alt_deg": alt,
            "az_deg": mst.az_deg,
            "airmass": airmass,
            "filter": wst.name if wst else None,
            "focus_pos": fst.position if fst else None,
        }
        return author_fits(raw, ctx)

    def snapshot(self) -> dict:
        ready = self.connected and self.mount is not None and self.camera is not None
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "indi_connected": self.connected,
            "last_image_at": self.latest_image_at,
            "mount": self.mount.status().dict() if ready else None,
            "camera": self.camera.status().dict() if ready else None,
            "focuser": self.focuser.status().dict() if (ready and self.focuser) else None,
            "filter": self.filterwheel.status().dict() if (ready and self.filterwheel) else None,
        }
