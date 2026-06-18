"""Observatory definition loaded from ``observatory.yaml``.

One YAML file describes a single observatory: its identity, geographic location,
the INDI server that fronts its instruments, and the equipment on the pier. The
*location* is the important part operationally — it is pushed to the mount's
``GEOGRAPHIC_COORD`` on connect so reported RA/Dec are actually correct (without
it the mount computes Local Sidereal Time at longitude 0 and every RA is wrong).

Everything else (equipment names, optics, sensors) is descriptive: it documents
the rig and feeds FITS provenance. Role→device *bindings* are still chosen at
runtime in the console — this file does not bind anything.
"""
from __future__ import annotations

import logging
import pathlib

import yaml
from pydantic import BaseModel

log = logging.getLogger("cassa.observatory")


class Location(BaseModel):
    latitude_deg: float = 0.0      # +north
    longitude_deg: float = 0.0     # +east (IAU/INDI convention)
    elevation_m: float = 0.0
    timezone: str | None = None    # IANA name, e.g. "Asia/Dhaka" (informational)
    utc_offset_hours: float = 0.0  # used for the mount's TIME_UTC offset

    @property
    def is_set(self) -> bool:
        return bool(self.latitude_deg or self.longitude_deg)


class Optic(BaseModel):
    name: str | None = None
    aperture_mm: float | None = None
    focal_length_mm: float | None = None


class DeviceSpec(BaseModel):
    name: str | None = None
    indi_device: str | None = None   # expected INDI device label (documentation)
    driver: str | None = None        # INDI driver binary, e.g. indi_eqmod_telescope
    port: str | None = None          # serial port, for serial gear
    baud: int | None = None


class CameraSpec(DeviceSpec):
    role: str = "camera"             # "camera" (science) or "guide"
    sensor: str | None = None
    pixel_size_um: float | None = None


class FilterWheelSpec(DeviceSpec):
    filters: list[str] = []          # slot labels in order


class Equipment(BaseModel):
    mount: DeviceSpec = DeviceSpec()
    telescope: Optic = Optic()
    cameras: list[CameraSpec] = []
    focuser: DeviceSpec = DeviceSpec()
    filter_wheel: FilterWheelSpec = FilterWheelSpec()


class IndiServer(BaseModel):
    host: str = "localhost"
    port: int = 7624


class Weather(BaseModel):
    """Site conditions shown on the locations dashboard. Static in config for now;
    a real ObservingConditions/weather source plugs in here later."""
    condition: str | None = None     # "Clear Sky", "Cloudy", "Rain", …
    seeing: str | None = None        # "Excellent", "Good", "Poor", …
    humidity: float | None = None    # %
    temperature: float | None = None  # °C


class Telescope(BaseModel):
    """One rig at a site, fronted by its own INDI server (host:port)."""
    id: str = "main"
    name: str | None = None
    indi: IndiServer = IndiServer()


class Observatory(BaseModel):
    id: str = "cassa"
    name: str = "CASSA Observatory"
    observer: str = "CASSA"
    instrument_id: str = "instr"
    status: str = "online"           # online | maintenance | offline
    location: Location = Location()
    indi: IndiServer = IndiServer()
    equipment: Equipment = Equipment()
    weather: Weather | None = None
    telescopes: list[Telescope] = []

    def camera(self, role: str = "camera") -> CameraSpec | None:
        return next((c for c in self.equipment.cameras if c.role == role), None)

    def telescopes_view(self) -> list[dict]:
        """The site's telescopes (each with its INDI endpoint). If none are declared,
        synthesize one from the site-level INDI server + mount name (back-compat)."""
        if self.telescopes:
            return [{"id": t.id, "name": t.name or t.id,
                     "indi_host": t.indi.host, "indi_port": t.indi.port}
                    for t in self.telescopes]
        return [{"id": "main", "name": self.equipment.mount.name or self.name,
                 "indi_host": self.indi.host, "indi_port": self.indi.port}]


def load_observatory(path: str | pathlib.Path) -> Observatory:
    """Parse ``observatory.yaml``. Missing/empty file → defaults (no site location,
    logged loudly because that means the mount's RA/Dec will be wrong)."""
    p = pathlib.Path(path)
    if not p.exists():
        log.warning("observatory file %s not found — using defaults; mount will have "
                    "no site location and RA/Dec will be inaccurate", p)
        return Observatory()
    data = yaml.safe_load(p.read_text()) or {}
    # Accept an optional top-level `observatory:` wrapper for readability.
    if isinstance(data.get("observatory"), dict):
        wrapper = data.pop("observatory")
        data = {**wrapper, **data}
    obs = Observatory(**data)
    log.info("observatory loaded: %s (%s) lat=%.4f long=%.4f", obs.name, obs.id,
             obs.location.latitude_deg, obs.location.longitude_deg)
    return obs
