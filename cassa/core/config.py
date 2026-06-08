"""Configuration: env vars (CASSA_*) plus an optional site YAML file.

The site YAML is the config-driven device map. Swapping from the virtual site to
real hardware is just editing this file (device names) — no code change.
"""
from __future__ import annotations

import logging
import pathlib

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("cassa.config")


class Settings(BaseSettings):
    indi_host: str = "localhost"
    indi_port: int = 7624
    site_config: str = "sites/virtual.yaml"

    mount_device: str = "Telescope Simulator"
    camera_device: str = "CCD Simulator"
    focuser_device: str = "Focuser Simulator"
    filterwheel_device: str = "Filter Simulator"

    # identity baked into FITS provenance + obsids
    site_id: str = "virtual"
    instrument_id: str = "vinstr"
    observer: str = "CASSA"
    telescope_name: str = "Telescope Simulator"
    instrument_name: str = "CCD Simulator"

    # archive
    db_url: str = "sqlite+aiosqlite:///data/cassa.db"
    data_dir: str = "data/store"

    model_config = SettingsConfigDict(env_prefix="CASSA_", env_file=".env", extra="ignore")


def load_settings() -> Settings:
    s = Settings()
    path = pathlib.Path(s.site_config)
    if not path.exists():
        log.info("no site config at %s; using defaults/env", path)
        return s
    cfg = yaml.safe_load(path.read_text()) or {}
    indi = cfg.get("indi", {})
    s.indi_host = indi.get("host", s.indi_host)
    s.indi_port = int(indi.get("port", s.indi_port))
    site = cfg.get("site", {})
    if site.get("id"):
        s.site_id = site["id"]
    role_to_field = {
        "mount": "mount_device",
        "camera": "camera_device",
        "focuser": "focuser_device",
        "filterwheel": "filterwheel_device",
    }
    for dev in cfg.get("devices", []):
        field = role_to_field.get(dev.get("role"))
        if field and dev.get("indi_device"):
            setattr(s, field, dev["indi_device"])
    log.info("loaded site config %s (mount=%s, camera=%s, focuser=%s, wheel=%s)",
             path, s.mount_device, s.camera_device, s.focuser_device, s.filterwheel_device)
    return s
