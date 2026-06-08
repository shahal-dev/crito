"""INDI implementations of the device roles (Mount, Camera, Focuser, FilterWheel).

These map the vendor-neutral role methods onto standard INDI properties. The same
adapter drives the simulator drivers and the real EQ6-R (``indi_eqmod``) / ToupTek
(``indi_toupbase``) — only the device names in config differ.
"""
from __future__ import annotations

import asyncio
import logging

from .imaging import fits_to_png
from .indi.protocol import INDIClient
from .roles import CameraStatus, FilterWheelStatus, FocuserStatus, MountStatus

log = logging.getLogger("cassa.dal")


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
    def __init__(self, client: INDIClient, device: str):
        self.client = client
        self.device = device

    async def connect(self, timeout: float = 15.0) -> None:
        await _connect_device(self.client, self.device, timeout)
        await self.client.wait_for(
            lambda: self.client.has_prop(self.device, "EQUATORIAL_EOD_COORD"), timeout
        )
        log.info("mount %s connected", self.device)

    async def slew_to_radec(self, ra_hours: float, dec_deg: float, track: bool = True) -> None:
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

    async def abort(self) -> None:
        await self.client.set_switch(self.device, "TELESCOPE_ABORT_MOTION", {"ABORT": True})

    async def park(self, park: bool = True) -> None:
        await self.client.set_switch(
            self.device, "TELESCOPE_PARK", {"PARK": park, "UNPARK": not park}
        )

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

    def _names(self) -> list[str]:
        try:
            elems = self.client._state[self.device]["FILTER_NAME"]["elements"]
        except KeyError:
            return []
        # element names look like FILTER_NAME_1, FILTER_NAME_2 ... keep them ordered
        return [elems[k] for k in sorted(elems, key=lambda s: int(s.rsplit("_", 1)[-1])
                                         if s.rsplit("_", 1)[-1].isdigit() else 0)]

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
