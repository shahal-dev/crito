"""Vendor-neutral device roles. The rest of CRITO talks to these, never to INDI."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import List, Optional


@dataclass
class MountStatus:
    connected: bool
    ra_hours: Optional[float]
    dec_deg: Optional[float]
    alt_deg: Optional[float]
    az_deg: Optional[float]
    slewing: bool
    tracking: bool
    parked: bool

    def dict(self) -> dict:
        return asdict(self)


@dataclass
class CameraStatus:
    connected: bool
    exposing: bool
    exposure_remaining: float

    def dict(self) -> dict:
        return asdict(self)


@dataclass
class FocuserStatus:
    connected: bool
    position: Optional[float]
    moving: bool

    def dict(self) -> dict:
        return asdict(self)


@dataclass
class FilterWheelStatus:
    connected: bool
    position: Optional[int]  # 1-based slot
    name: Optional[str]
    names: List[str] = field(default_factory=list)
    moving: bool = False

    def dict(self) -> dict:
        return asdict(self)
