"""Configuration via environment variables (``CRITO_*``) or a ``.env`` file.

There is no device map in config: CRITO discovers whatever devices the INDI
server exposes and you bind each role (mount/camera/focuser/filter) to a real
device at runtime from the web console. Those bindings persist to
``bindings_path``. The only config here is where the INDI server lives plus the
identity stamped into FITS provenance and obsids.
"""
from __future__ import annotations

import logging
import os

from pydantic_settings import BaseSettings, SettingsConfigDict

from .observatory import Observatory, load_observatory

log = logging.getLogger("crito.config")


class Settings(BaseSettings):
    # INDI server (typically a remote edge node at the observatory). Can also be
    # repointed at runtime from the console; the chosen host/port persist with
    # the device bindings.
    indi_host: str = "localhost"
    indi_port: int = 7624

    # identity baked into FITS provenance + obsids
    site_id: str = "crito"
    instrument_id: str = "instr"
    observer: str = "CRITO"
    # fallbacks used only when no mount/camera is bound yet
    telescope_name: str = "Unknown"
    instrument_name: str = "Unknown"

    # observatory definition (identity, location, equipment) — see observatory.yaml
    observatory_file: str = "observatory.yaml"
    # site location, projected from the observatory file; pushed to the mount so
    # RA/Dec are correct. Defaults of 0 mean "no location configured".
    latitude_deg: float = 0.0
    longitude_deg: float = 0.0     # +east
    elevation_m: float = 0.0
    utc_offset_hours: float = 0.0

    # archive
    db_url: str = "sqlite+aiosqlite:///data/crito.db"
    data_dir: str = "data/store"

    # runtime device bindings (role -> device) chosen from the UI, persisted here
    bindings_path: str = "data/bindings.json"

    # PHD2 guiding event server (runs on the edge node with the guide cam + mount).
    # Empty host → use the INDI host. Enable in PHD2: Tools → Enable Server.
    phd2_host: str = ""
    phd2_port: int = 4400

    # --- weather + safety FSM ----------------------------------------------
    safety_enabled: bool = True            # enforce the safety state machine
    weather_device: str = ""               # optional INDI weather device label to read
    # Weather API auto-feed (uses the site's lat/lon). "open-meteo" (free, no key),
    # "openweather" (needs key), or "" to disable. A regional API is a COARSE safety
    # input — pair it with an on-site rain/cloud sensor for real protection.
    weather_api: str = "open-meteo"
    weather_api_key: str = ""              # for openweather
    weather_poll_s: int = 600
    safety_stale_s: float = 180.0          # weather older than this → UNSAFE (stale = unsafe)
    safety_clear_delay_s: float = 120.0    # conditions must hold OK this long before SAFE (hysteresis)
    safety_humidity_warn: float = 85.0     # %
    safety_humidity_unsafe: float = 95.0
    safety_wind_unsafe: float = 40.0       # km/h
    safety_cloud_unsafe: float = 90.0      # cloud cover %, if the source reports it

    # --- precision: plate-solve + autofocus (ASTAP) ------------------------
    solver: str = "astap"                  # "astap" | "none" (disables center/autofocus)
    astap_path: str = "astap"              # ASTAP binary (on PATH or absolute)
    solve_db: str = ""                     # ASTAP star DB dir (-d); "" = default install
    astap_search_radius_deg: float = 30.0  # solve search radius around the hint
    astap_downsample: int = 0              # -z downsample (0 = auto)
    solve_exposure_s: float = 4.0          # exposure for a plate-solve frame
    center_tolerance_arcsec: float = 30.0  # "centered" when offset is below this
    center_max_iter: int = 3               # solve→sync→reslew iterations
    solve_science_frames: bool = False     # also solve each LIGHT frame → write WCS (slower)
    # optics for the FOV hint + plate scale — projected from observatory.yaml
    focal_length_mm: float = 0.0           # 0 = unknown → ASTAP auto-detects FOV
    pixel_size_um: float = 0.0
    # exposure/SNR planner: per-gain & per-filter sensor constants (crito.calib).
    # Point CRITO_CALIBRATION_FILE at your measured table; the example ships values.
    calibration_file: str = "calibration/minicam8.example.yaml"
    # autofocus (HFR V-curve)
    af_exposure_s: float = 3.0
    af_step_size: int = 100                # focuser steps between samples
    af_steps: int = 9                      # samples per sweep (odd)
    af_backlash: int = 200                 # steps for one-directional final approach
    af_min_stars: int = 5                  # need at least this many stars to trust a sample
    af_min_snr: float = 30.0               # ASTAP -analyse SNR threshold
    # calibration: opaque/"dark" filter slot moved into place for dark/bias frames.
    # 0 = auto-detect by name (a slot called dark/blank/opaque/shutter, e.g. on the
    # QHY MiniCam8 wheel); set explicitly to force a slot.
    dark_filter_slot: int = 0

    # --- auth / RBAC --------------------------------------------------------
    # Set the SAME secret on every site backend so one login works across sites.
    auth_secret: str = "change-me-in-production"
    admin_user: str = "admin"        # default admin seeded on first run
    admin_password: str = "admin"    # CHANGE after first login

    # --- transient follow-up pipeline ---------------------------------------
    # ALeRCE broker ingest
    alerce_poll_s: int = 600                 # poll cadence (s); >=30 enforced
    alerce_lookback_days: float = 7.0        # only ingest objects active within N days
    # stamp_classifier labels fresh alerts (1st detection) → catches new transients;
    # lc_classifier needs >=6 detections so it misses young SNe. Empty = no classifier
    # (recent objects, class "unknown").
    alerce_classifier: str = "stamp_classifier"
    alerce_classes: str = "SN,AGN,VS"        # comma-sep classes to pull; "" = no class filter
    alerce_probability: float = 0.4          # min classifier probability
    alerce_page_size: int = 100
    alerce_max_pages: int = 2
    alerce_min_ndet: int = 0                 # 0 = no minimum detection count
    alerce_timeout_s: float = 60.0           # ALeRCE /objects (lastmjd sort) can be slow
    # visibility filter (IUB Dhaka from observatory.yaml)
    alt_min_deg: float = 30.0                # horizon limit — the "30° horizon"
    moon_sep_min_deg: float = 30.0           # soft penalty, not a hard cut
    mag_limit: float = 18.5                  # instrument magnitude reach (too-faint cut)
    # scoring weights (figure of merit)
    score_w_prob: float = 1.0
    score_w_alt: float = 1.0
    score_w_airmass: float = 0.5
    score_w_moon: float = 0.5
    score_w_faint: float = 0.5
    # default imaging recipe applied on approval (mono ToupTek: luminance N×exptime)
    default_exptime_s: float = 120.0
    default_count: int = 5
    default_filter_slot: int | None = None   # None = whatever filter is in place
    # approval — Slack bot (Socket Mode) + email fallback (Phase D)
    slack_bot_token: str = ""                # xoxb-… (bot)
    slack_app_token: str = ""                # xapp-… (Socket Mode)
    slack_channel: str = ""                  # channel id to post candidate cards to
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""                        # supervisor address(es), comma-separated
    approve_secret: str = "change-me"        # HMAC secret for signed email approval links
    console_base_url: str = "http://localhost:5173"
    api_base_url: str = "http://localhost:8000"  # where email approval deep-links point
    # execution (Phase F/G)
    auto_execute: bool = False               # guarded auto-dispatch; OFF until safety FSM

    model_config = SettingsConfigDict(env_prefix="CRITO_", env_file=".env", extra="ignore")


def apply_observatory(settings: Settings, obs: Observatory) -> Settings:
    """Project an observatory definition onto Settings. Explicit ``CRITO_*`` env
    vars win, so a deploy-time override is never shadowed by the file."""
    def unset(name: str) -> bool:
        return f"CRITO_{name}" not in os.environ

    if unset("SITE_ID"):
        settings.site_id = obs.id
    if unset("OBSERVER"):
        settings.observer = obs.observer
    if unset("INSTRUMENT_ID"):
        settings.instrument_id = obs.instrument_id
    if unset("INDI_HOST"):
        settings.indi_host = obs.indi.host
    if unset("INDI_PORT"):
        settings.indi_port = obs.indi.port
    if unset("TELESCOPE_NAME") and obs.equipment.mount.name:
        settings.telescope_name = obs.equipment.mount.name
    if unset("INSTRUMENT_NAME"):
        cam = obs.camera("camera")
        if cam and cam.name:
            settings.instrument_name = cam.name
    # optics for plate-solve FOV / scale (env still wins)
    if unset("FOCAL_LENGTH_MM") and obs.equipment.telescope.focal_length_mm:
        settings.focal_length_mm = obs.equipment.telescope.focal_length_mm
    if unset("PIXEL_SIZE_UM"):
        scam = obs.camera("camera")
        if scam and scam.pixel_size_um:
            settings.pixel_size_um = scam.pixel_size_um

    settings.latitude_deg = obs.location.latitude_deg
    settings.longitude_deg = obs.location.longitude_deg
    settings.elevation_m = obs.location.elevation_m
    settings.utc_offset_hours = obs.location.utc_offset_hours
    return settings


def load_settings() -> tuple[Settings, Observatory]:
    s = Settings()
    obs = load_observatory(s.observatory_file)
    apply_observatory(s, obs)
    log.info("config loaded — INDI %s:%s, site=%s, location=(%.4f, %.4f)",
             s.indi_host, s.indi_port, s.site_id, s.latitude_deg, s.longitude_deg)
    return s, obs
