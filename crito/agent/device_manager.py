"""Device Manager: INDI transport + runtime, UI-driven device bindings.

The INDI *transport* (connection to ``indiserver``) auto-connects and retries. Which
discovered device fills each *role* (mount/camera/focuser/filter) is decided at
runtime from the frontend and persisted to ``bindings.json`` — no hardcoded device
names. Pick the real devices in the console; bindings persist to ``bindings.json``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import pathlib
from datetime import datetime, timezone

from .. import __version__
from ..dal.indi.protocol import INDIClient
from ..dal.indi_adapter import IndiCamera, IndiFilterWheel, IndiFocuser, IndiMount
from .fits_writer import author_fits

log = logging.getLogger("crito.agent")

_RECONNECT_DELAY = 2.0

# role -> attribute on this manager
ROLE_ATTR = {
    "mount": "mount",
    "camera": "camera",
    "guide": "guider",
    "focuser": "focuser",
    "filter": "filterwheel",
}

# INDI DRIVER_INTERFACE bitmask -> roles we care about. A single device may set
# several bits (e.g. a QHY camera+filter bundle reports CCD|FILTER, an imaging
# camera reports CCD|GUIDER) — every matching role becomes a candidate the UI can
# bind that one device to independently.
_IFACE_ROLES = [(1, "mount"), (2, "camera"), (4, "guide"), (8, "focuser"), (16, "filter")]


def _roles_for(iface: int) -> list[str]:
    roles: list[str] = []
    for bit, role in _IFACE_ROLES:
        if iface & bit and role not in roles:
            roles.append(role)
    return roles


class DeviceManager:
    def __init__(self, settings):
        self.settings = settings
        self.mount: IndiMount | None = None
        self.camera: IndiCamera | None = None
        self.guider: IndiCamera | None = None
        self.focuser: IndiFocuser | None = None
        self.filterwheel: IndiFilterWheel | None = None
        self.connected = False
        self.latest_png: bytes | None = None
        self.latest_image_at: str | None = None
        self.latest_guide_png: bytes | None = None
        self.latest_guide_image_at: str | None = None
        self._bindings: dict[str, dict] = {}
        self._server = {"host": settings.indi_host, "port": settings.indi_port}
        self._load_bindings()
        # An explicit CRITO_INDI_HOST/PORT in the environment always wins over a
        # server persisted in bindings.json (e.g. a stale "Connect server" choice),
        # so a deploy-time override is never silently shadowed by an old binding.
        if os.environ.get("CRITO_INDI_HOST"):
            self._server["host"] = settings.indi_host
        if os.environ.get("CRITO_INDI_PORT"):
            self._server["port"] = settings.indi_port
        self.client = INDIClient(self._server["host"], self._server["port"])
        self._stop = False
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ persistence
    def _load_bindings(self) -> None:
        path = pathlib.Path(self.settings.bindings_path)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            self._server.update(data.get("server", {}))
            self._bindings = data.get("bindings", {})
            log.info("loaded %d device binding(s) from %s", len(self._bindings), path)
        except Exception:
            log.exception("could not read bindings file %s", path)

    def _save_bindings(self) -> None:
        path = pathlib.Path(self.settings.bindings_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"server": self._server, "bindings": self._bindings}, indent=2))

    # ------------------------------------------------------------- lifecycle
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
                self.connected = True
                log.info("INDI transport up (%s:%s)", self._server["host"], self._server["port"])
                await self._rebind_all()
                await self.client.wait_closed()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("INDI transport problem: %s", e)
            finally:
                self.connected = False
                self.mount = self.camera = self.guider = self.focuser = self.filterwheel = None
            if self._stop:
                break
            await asyncio.sleep(_RECONNECT_DELAY)

    async def set_server(self, host: str, port: int) -> None:
        """Repoint the INDI transport (e.g. at a remote edge node) and reconnect.

        If we're already connected to this exact endpoint, do nothing — bouncing a
        healthy link would briefly drop every binding and flash "INDI down" for no
        reason.
        """
        port = int(port)
        if self.connected and self._server == {"host": host, "port": port}:
            return
        self._server = {"host": host, "port": port}
        self._save_bindings()
        old = self.client
        self.client = INDIClient(host, int(port))  # next _run iteration uses this
        await old.close()  # unblocks wait_closed -> loop reconnects with the new client

    # --------------------------------------------------------------- binding
    def _mount_site(self) -> dict | None:
        """Observatory location for the mount, or None when unconfigured (so we
        never clobber the mount with a bogus 0,0)."""
        s = self.settings
        lat = getattr(s, "latitude_deg", 0.0) or 0.0
        lng = getattr(s, "longitude_deg", 0.0) or 0.0
        if not lat and not lng:
            return None
        return {
            "lat": lat,
            "long": lng,
            "elev": getattr(s, "elevation_m", 0.0) or 0.0,
            "offset": getattr(s, "utc_offset_hours", 0.0) or 0.0,
        }

    def _make_adapter(self, role: str, device: str):
        if role == "mount":
            return IndiMount(self.client, device, site=self._mount_site())
        if role == "camera":
            return IndiCamera(self.client, device, on_image=self._on_image)
        if role == "guide":
            # Guide frames feed their own preview, separate from the science camera.
            return IndiCamera(self.client, device, on_image=self._on_guide_image)
        if role == "focuser":
            return IndiFocuser(self.client, device)
        if role == "filter":
            return IndiFilterWheel(self.client, device)
        raise ValueError(f"unknown role {role!r}")

    async def _bind_internal(self, role: str, device: str, params: dict | None) -> None:
        if not await self.client.wait_for(lambda: self.client.has_prop(device, "CONNECTION"), 15):
            raise TimeoutError(f"device {device!r} is not present on the INDI server")
        if params:  # e.g. {"DEVICE_PORT": {"PORT": "/dev/ttyUSB0"}}
            for prop, elems in params.items():
                await self.client.set_property(device, prop, elems)
        adapter = self._make_adapter(role, device)
        await adapter.connect()
        setattr(self, ROLE_ATTR[role], adapter)
        log.info("bound %s -> %s", role, device)

    async def _rebind_all(self) -> None:
        for role, b in list(self._bindings.items()):
            try:
                await self._bind_internal(role, b["device"], b.get("params"))
            except Exception as e:
                log.warning("could not restore %s -> %s: %s", role, b.get("device"), e)

    async def bind(self, role: str, device: str, params: dict | None = None) -> dict:
        if role not in ROLE_ATTR:
            raise ValueError(f"unknown role {role!r}")
        if not self.connected:
            raise RuntimeError("INDI server not connected")
        await self._bind_internal(role, device, params or {})
        self._bindings[role] = {"device": device, "params": params or {}}
        self._save_bindings()
        return {"role": role, "device": device}

    async def unbind(self, role: str) -> None:
        if role not in ROLE_ATTR:
            raise ValueError(f"unknown role {role!r}")
        adapter = getattr(self, ROLE_ATTR[role])
        if adapter is not None:
            try:
                await self.client.set_switch(
                    adapter.device, "CONNECTION", {"CONNECT": False, "DISCONNECT": True}
                )
            except Exception:
                log.debug("disconnect of %s failed", role, exc_info=True)
        setattr(self, ROLE_ATTR[role], None)
        self._bindings.pop(role, None)
        self._save_bindings()

    async def autodetect(self) -> list[dict]:
        """Bind each unassigned discovered device to its primary interface role."""
        bound = []
        for d in self.list_devices():
            if d["bound_as"]:
                continue
            for role in d["roles"]:
                if getattr(self, ROLE_ATTR[role]) is None:
                    try:
                        await self.bind(role, d["device"])
                        bound.append({"role": role, "device": d["device"]})
                        break
                    except Exception as e:
                        log.warning("autodetect %s -> %s failed: %s", role, d["device"], e)
        return bound

    # ------------------------------------------------------------- discovery
    def list_devices(self) -> list[dict]:
        out = []
        for dev in self.client.device_names():
            iface_str = self.client.element(dev, "DRIVER_INFO", "DRIVER_INTERFACE")
            try:
                iface = int(iface_str) if iface_str is not None else 0
            except (TypeError, ValueError):
                iface = 0
            out.append({
                "device": dev,
                "roles": _roles_for(iface),
                "connected": bool(self.client.element(dev, "CONNECTION", "CONNECT", False)),
                # CONNECTION property state: "Ok" while healthy, "Alert" when the
                # driver hit an error (e.g. the serial port vanished on unplug) —
                # lets the UI flag a dropped device even if CONNECT is still On.
                "conn_state": self.client.prop_state(dev, "CONNECTION"),
                "bound_as": next((r for r, b in self._bindings.items() if b["device"] == dev), None),
                "has_port": self.client.has_prop(dev, "DEVICE_PORT"),
                "port": self.client.element(dev, "DEVICE_PORT", "PORT"),
            })
        return out

    # --------------------------------------------------------------- imaging
    def _on_image(self, png: bytes) -> None:
        self.latest_png = png
        self.latest_image_at = datetime.now(timezone.utc).isoformat()

    def _on_guide_image(self, png: bytes) -> None:
        self.latest_guide_png = png
        self.latest_guide_image_at = datetime.now(timezone.utc).isoformat()

    async def capture(self, seconds: float, image_type: str = "LIGHT",
                      object_name: str = "", filter_slot: int | None = None,
                      role: str = "camera", binning: int | None = None) -> dict:
        if role not in ("camera", "guide"):
            raise ValueError(f"{role!r} is not a camera role")
        cam = getattr(self, ROLE_ATTR[role])
        if not (self.connected and self.mount and cam):
            raise RuntimeError(f"mount and {role} must be connected to capture")

        if binning:
            try:
                await cam.set_binning(int(binning))
            except Exception:
                log.debug("set binning %s failed", binning, exc_info=True)

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

        raw = await cam.capture(seconds)
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
            "telescope": self.mount.device if self.mount else self.settings.telescope_name,
            "instrument": cam.device,
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

    # -------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        on = self.connected
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "indi_connected": on,
            "server": dict(self._server),
            "last_image_at": self.latest_image_at,
            "last_guide_image_at": self.latest_guide_image_at,
            # Live device inventory so the console reflects hot-plug/unplug without
            # a manual rescan (pushed on every telemetry frame).
            "devices": self.list_devices() if on else [],
            "mount": self.mount.status().dict() if (on and self.mount) else None,
            "camera": self.camera.status().dict() if (on and self.camera) else None,
            "guider": self.guider.status().dict() if (on and self.guider) else None,
            "focuser": self.focuser.status().dict() if (on and self.focuser) else None,
            "filter": self.filterwheel.status().dict() if (on and self.filterwheel) else None,
            "bindings": {role: self._bindings.get(role, {}).get("device") for role in ROLE_ATTR},
        }
