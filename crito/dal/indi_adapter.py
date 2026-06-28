"""INDI implementations of the device roles (Mount, Camera, Focuser, FilterWheel).

These map the vendor-neutral role methods onto standard INDI properties, so the
same adapter drives any real INDI-supported device (mounts, CCD/CMOS cameras,
focusers, filter wheels) regardless of brand — the device is chosen at runtime.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .imaging import fits_to_png
from .indi.protocol import INDIClient
from .roles import CameraStatus, FilterWheelStatus, FocuserStatus, MountStatus

log = logging.getLogger("crito.dal")


async def _connect_device(client: INDIClient, device: str, timeout: float) -> None:
    if not await client.wait_for(lambda: client.has_prop(device, "CONNECTION"), timeout):
        raise TimeoutError(f"device {device!r} never appeared on the INDI server")
    if not client.element(device, "CONNECTION", "CONNECT"):
        await client.set_switch(device, "CONNECTION", {"CONNECT": True, "DISCONNECT": False})
    if not await client.wait_for(
        lambda: client.element(device, "CONNECTION", "CONNECT") is True, timeout
    ):
        raise TimeoutError(f"device {device!r} failed to connect")


class IndiMount:
    def __init__(self, client: INDIClient, device: str, site: dict | None = None):
        self.client = client
        self.device = device
        # {"lat": deg, "long": deg(+east), "elev": m, "offset": hours} or None
        self.site = site

    async def connect(self, timeout: float = 15.0) -> None:
        await _connect_device(self.client, self.device, timeout)
        await self.client.wait_for(
            lambda: self.client.has_prop(self.device, "EQUATORIAL_EOD_COORD"), timeout
        )
        if self.site:
            try:
                await self._apply_site()
            except Exception:
                log.warning("could not push site location to mount %s", self.device, exc_info=True)
        log.info("mount %s connected", self.device)

    async def _apply_site(self) -> None:
        """Set the mount's location + clock so RA/Dec are computed correctly.

        Without a real site location the mount computes Local Sidereal Time at
        longitude 0, so every reported RA is off by (longitude / 15) hours. INDI
        expects longitude in 0..360 east-positive.
        """
        lat = float(self.site["lat"])
        lng = float(self.site["long"]) % 360.0
        elev = float(self.site.get("elev", 0.0))
        await self.client.wait_for(
            lambda: self.client.has_prop(self.device, "GEOGRAPHIC_COORD"), 5.0
        )
        await self.client.set_number(
            self.device, "GEOGRAPHIC_COORD", {"LAT": lat, "LONG": lng, "ELEV": elev}
        )
        if self.client.has_prop(self.device, "TIME_UTC"):
            now = datetime.now(timezone.utc)
            await self.client.set_text(self.device, "TIME_UTC", {
                "UTC": now.strftime("%Y-%m-%dT%H:%M:%S"),
                "OFFSET": f"{float(self.site.get('offset', 0.0)):g}",
            })
        log.info("mount %s site set: lat=%.4f long=%.4f(+E) elev=%.0fm", self.device, lat, lng, elev)

    async def _ensure_unparked(self) -> None:
        """A parked (or freshly-connected, indeterminate) mount refuses to slew or
        track. Send UNPARK unless it is already explicitly unparked, and wait."""
        if not self.client.element(self.device, "TELESCOPE_PARK", "UNPARK"):
            await self.unpark()
            await self.client.wait_for(
                lambda: not self.client.element(self.device, "TELESCOPE_PARK", "PARK", False), 10.0
            )

    async def slew_to_radec(self, ra_hours: float, dec_deg: float, track: bool = True) -> None:
        await self._ensure_unparked()
        await self.client.set_switch(
            self.device, "ON_COORD_SET", {"TRACK": track, "SLEW": not track, "SYNC": False}
        )
        await self.client.set_number(
            self.device, "EQUATORIAL_EOD_COORD", {"RA": ra_hours, "DEC": dec_deg}
        )

    async def sync_to_radec(self, ra_hours: float, dec_deg: float) -> None:
        await self.client.set_switch(
            self.device, "ON_COORD_SET", {"TRACK": False, "SLEW": False, "SYNC": True}
        )
        await self.client.set_number(
            self.device, "EQUATORIAL_EOD_COORD", {"RA": ra_hours, "DEC": dec_deg}
        )

    async def set_tracking(self, on: bool) -> None:
        """Directly start/stop sidereal tracking (independent of any slew)."""
        if on:
            await self._ensure_unparked()
        await self.client.set_switch(
            self.device, "TELESCOPE_TRACK_STATE", {"TRACK_ON": on, "TRACK_OFF": not on}
        )

    def _home_radec(self) -> tuple[float, float]:
        """RA/Dec of the home/index position: counterweight down, optical tube on
        the celestial pole (HA=0). That is RA = current LST and Dec at the pole
        for this hemisphere. Requires a configured site for the LST."""
        if not self.site:
            raise RuntimeError("no site location configured — cannot compute Home")
        from astropy.time import Time
        import astropy.units as u
        lst = float(Time.now().sidereal_time("apparent", longitude=self.site["long"] * u.deg).hour)
        dec = 89.9 if self.site["lat"] >= 0 else -89.9
        return lst, dec

    async def go_home(self) -> None:
        """Slew the mount to its home/index position (counterweight down, optical
        tube on the celestial pole)."""
        ra, dec = self._home_radec()
        await self._ensure_unparked()
        await self.client.set_switch(
            self.device, "ON_COORD_SET", {"TRACK": False, "SLEW": True, "SYNC": False}
        )
        await self.client.set_number(
            self.device, "EQUATORIAL_EOD_COORD", {"RA": ra, "DEC": dec}
        )

    async def set_home(self) -> None:
        """Define the mount's *current* physical position as the home/index by
        syncing it to RA = current LST, Dec = pole. Use when the mount is sitting
        at counterweight-down / pointing-the-pole so the pointing model references
        that as HA=0. Unlike go_home() this moves nothing — it re-references."""
        ra, dec = self._home_radec()
        await self.sync_to_radec(ra, dec)

    async def set_park(self) -> None:
        """Save the mount's current position as the park position. PARK_CURRENT
        snapshots the live encoder counts into TELESCOPE_PARK_POSITION;
        PARK_WRITE_DATA persists them to the driver's ParkData.xml so every
        subsequent Park slews here. Two separate writes: load, then commit."""
        await self.client.set_switch(
            self.device, "TELESCOPE_PARK_OPTION", {"PARK_CURRENT": True}
        )
        await self.client.set_switch(
            self.device, "TELESCOPE_PARK_OPTION", {"PARK_WRITE_DATA": True}
        )

    async def abort(self) -> None:
        await self.client.set_switch(self.device, "TELESCOPE_ABORT_MOTION", {"ABORT": True})

    async def park(self, park: bool = True) -> None:
        await self.client.set_switch(
            self.device, "TELESCOPE_PARK", {"PARK": park, "UNPARK": not park}
        )

    async def unpark(self) -> None:
        await self.park(False)

    def status(self) -> MountStatus:
        c = self.client
        return MountStatus(
            connected=bool(c.element(self.device, "CONNECTION", "CONNECT", False)),
            ra_hours=c.element(self.device, "EQUATORIAL_EOD_COORD", "RA"),
            dec_deg=c.element(self.device, "EQUATORIAL_EOD_COORD", "DEC"),
            alt_deg=c.element(self.device, "HORIZONTAL_COORD", "ALT"),
            az_deg=c.element(self.device, "HORIZONTAL_COORD", "AZ"),
            slewing=c.prop_state(self.device, "EQUATORIAL_EOD_COORD") == "Busy",
            tracking=bool(c.element(self.device, "TELESCOPE_TRACK_STATE", "TRACK_ON", False)),
            parked=bool(c.element(self.device, "TELESCOPE_PARK", "PARK", False)),
        )


class IndiCamera:
    def __init__(self, client: INDIClient, device: str, on_image=None):
        self.client = client
        self.device = device
        self.on_image = on_image
        self._pending: asyncio.Future | None = None
        client.add_blob_handler(self._on_blob)

    async def connect(self, timeout: float = 15.0) -> None:
        await _connect_device(self.client, self.device, timeout)
        await self.client.enable_blob(self.device, "Also")  # required to receive image BLOBs
        log.info("camera %s connected", self.device)

    async def expose(self, seconds: float) -> None:
        await self.client.set_number(
            self.device, "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": seconds}
        )

    async def set_binning(self, binning: int) -> None:
        await self.client.set_number(
            self.device, "CCD_BINNING", {"HOR_BIN": int(binning), "VER_BIN": int(binning)}
        )

    async def capture(self, seconds: float, timeout: float | None = None) -> bytes:
        """Trigger an exposure and return the resulting raw FITS bytes."""
        loop = asyncio.get_event_loop()
        self._pending = loop.create_future()
        try:
            await self.expose(seconds)
            return await asyncio.wait_for(self._pending, timeout or (seconds + 60.0))
        finally:
            self._pending = None

    def _on_blob(self, device: str, name: str, ename: str, data: bytes, fmt: str) -> None:
        if device != self.device or not fmt.startswith(".fit"):
            return
        if self._pending and not self._pending.done():
            self._pending.set_result(data)
        if self.on_image:
            try:
                self.on_image(fits_to_png(data))
            except Exception:
                log.exception("FITS -> PNG preview failed for %s", device)

    def status(self) -> CameraStatus:
        c = self.client
        remaining = c.element(self.device, "CCD_EXPOSURE", "CCD_EXPOSURE_VALUE", 0.0)
        return CameraStatus(
            connected=bool(c.element(self.device, "CONNECTION", "CONNECT", False)),
            exposing=c.prop_state(self.device, "CCD_EXPOSURE") == "Busy",
            exposure_remaining=float(remaining or 0.0),
        )


class IndiFocuser:
    def __init__(self, client: INDIClient, device: str):
        self.client = client
        self.device = device

    async def connect(self, timeout: float = 10.0) -> None:
        await _connect_device(self.client, self.device, timeout)
        log.info("focuser %s connected", self.device)

    async def move_absolute(self, position: float) -> None:
        await self.client.set_number(
            self.device, "ABS_FOCUS_POSITION", {"FOCUS_ABSOLUTE_POSITION": position}
        )

    async def move_relative(self, steps: float, inward: bool = False) -> None:
        await self.client.set_switch(
            self.device, "FOCUS_MOTION", {"FOCUS_INWARD": inward, "FOCUS_OUTWARD": not inward}
        )
        await self.client.set_number(
            self.device, "REL_FOCUS_POSITION", {"FOCUS_RELATIVE_POSITION": steps}
        )

    async def abort(self) -> None:
        await self.client.set_switch(self.device, "FOCUS_ABORT_MOTION", {"ABORT": True})

    def status(self) -> FocuserStatus:
        c = self.client
        return FocuserStatus(
            connected=bool(c.element(self.device, "CONNECTION", "CONNECT", False)),
            position=c.element(self.device, "ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION"),
            moving=c.prop_state(self.device, "ABS_FOCUS_POSITION") == "Busy",
        )


class IndiFilterWheel:
    def __init__(self, client: INDIClient, device: str):
        self.client = client
        self.device = device

    async def connect(self, timeout: float = 10.0) -> None:
        await _connect_device(self.client, self.device, timeout)
        await self.client.wait_for(
            lambda: self.client.has_prop(self.device, "FILTER_SLOT"), timeout
        )
        log.info("filter wheel %s connected", self.device)

    async def set_position(self, slot: int) -> None:
        await self.client.set_number(self.device, "FILTER_SLOT", {"FILTER_SLOT_VALUE": float(slot)})

    async def set_name(self, slot: int, name: str) -> None:
        """Rename the filter in `slot` (1-based). FILTER_NAME is a writable text
        vector; we update just that slot's element and leave the rest untouched."""
        keys = self._name_keys()
        if not 1 <= slot <= len(keys):
            raise ValueError(f"filter slot {slot} out of range 1..{len(keys)}")
        await self.client.set_text(self.device, "FILTER_NAME", {keys[slot - 1]: name})

    def _name_keys(self) -> list[str]:
        """FILTER_NAME element keys ordered by slot (FILTER_SLOT_NAME_1, _2, ...)."""
        try:
            elems = self.client._state[self.device]["FILTER_NAME"]["elements"]
        except KeyError:
            return []
        return sorted(elems, key=lambda s: int(s.rsplit("_", 1)[-1])
                      if s.rsplit("_", 1)[-1].isdigit() else 0)

    def _names(self) -> list[str]:
        try:
            elems = self.client._state[self.device]["FILTER_NAME"]["elements"]
        except KeyError:
            return []
        return [elems[k] for k in self._name_keys()]

    def status(self) -> FilterWheelStatus:
        c = self.client
        slot = c.element(self.device, "FILTER_SLOT", "FILTER_SLOT_VALUE")
        pos = int(slot) if slot is not None else None
        names = self._names()
        name = names[pos - 1] if (pos and 1 <= pos <= len(names)) else None
        return FilterWheelStatus(
            connected=bool(c.element(self.device, "CONNECTION", "CONNECT", False)),
            position=pos,
            name=name,
            names=names,
            moving=c.prop_state(self.device, "FILTER_SLOT") == "Busy",
        )
