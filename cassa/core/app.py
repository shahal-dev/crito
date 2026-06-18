"""CASSA Core API (Phase 1).

Manual control (mount, camera, focuser, filter wheel), full-frame capture with FITS
authoring + archive ingest, an image archive (search/download/preview), and a live
telemetry WebSocket. Run with:  uvicorn cassa.core.app:app --reload
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..agent.device_manager import DeviceManager
from ..agent.phd2 import PHD2Client
from ..agent.precision import PrecisionOps
from ..agent.safety import SafetyMonitor
from ..agent.weather_api import WeatherApiPoller
from .activity import ActivityLog
from .auth import AuthService, ROLES, decode_token, make_token, role_rank
from ..transient.alerce import AlerceClient
from ..transient.approvals import ApprovalService
from ..transient.candidates import CandidateService
from ..transient.executor import ExecutionSequencer
from ..transient.plans import PlanService
from ..transient.poller import AlertPoller
from ..transient.requests import RequestBuilder
from .archive import ArchiveService
from .config import load_settings
from .db import DB
from . import transient_db  # noqa: F401 — registers transient tables on Base
from .storage import LocalStore
from .transient_routes import router as transient_router

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("cassa.core")

_TELEMETRY_HZ = 2.0


class SlewReq(BaseModel):
    ra_hours: float = Field(ge=0, lt=24)
    dec_deg: float = Field(ge=-90, le=90)
    track: bool = True


class TrackReq(BaseModel):
    on: bool


class ExposeReq(BaseModel):
    seconds: float = Field(gt=0, le=3600)


class CaptureReq(BaseModel):
    seconds: float = Field(gt=0, le=3600)
    image_type: str = "LIGHT"
    object_name: str = ""
    filter_slot: Optional[int] = Field(default=None, ge=1, le=64)


class FocusAbsReq(BaseModel):
    position: float = Field(ge=0)


class FocusRelReq(BaseModel):
    steps: float = Field(gt=0)
    inward: bool = False


class FilterReq(BaseModel):
    slot: int = Field(ge=1, le=64)


class FilterNameReq(BaseModel):
    slot: int = Field(ge=1, le=64)
    name: str = Field(min_length=1, max_length=64)


class BindReq(BaseModel):
    role: str = Field(pattern="^(mount|camera|guide|focuser|filter)$")
    device: str
    params: Optional[dict] = None  # e.g. {"DEVICE_PORT": {"PORT": "/dev/ttyUSB0"}}


class UnbindReq(BaseModel):
    role: str = Field(pattern="^(mount|camera|guide|focuser|filter)$")


class ServerReq(BaseModel):
    host: str
    port: int = Field(gt=0, le=65535)


async def _broadcaster(app: FastAPI) -> None:
    period = 1.0 / _TELEMETRY_HZ
    while True:
        await asyncio.sleep(period)
        clients = app.state.clients
        if not clients:
            continue
        snap = app.state.dm.snapshot()
        ex = getattr(app.state, "executor", None)
        if ex is not None:
            snap["executor"] = ex.snapshot()
        phd2 = getattr(app.state, "phd2", None)
        if phd2 is not None:
            snap["guiding"] = phd2.summary()
        safety = getattr(app.state, "safety", None)
        if safety is not None:
            snap["safety"] = safety.snapshot()
        precision = getattr(app.state, "precision", None)
        if precision is not None:
            snap["precision"] = precision.snapshot()
        for ws in list(clients):
            try:
                await ws.send_json(snap)
            except Exception:
                clients.discard(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings, observatory = load_settings()
    pathlib.Path("data").mkdir(exist_ok=True)

    store = LocalStore(settings.data_dir)
    db = DB(settings.db_url)
    await db.init()
    app.state.settings = settings
    app.state.observatory = observatory
    app.state.store = store
    app.state.db = db
    app.state.archive = ArchiveService(store, db.sessionmaker)
    app.state.dm = DeviceManager(settings)
    app.state.clients = set()
    app.state.activity = ActivityLog()

    await app.state.dm.start()

    # --- transient follow-up pipeline (broker ingest + visibility + candidates)
    # follow_redirects: ALeRCE redirects some requests; without this httpx returns the
    # 3xx, .json() fails on the redirect body, and the poller silently gets 0 objects.
    # timeout: ALeRCE's lastmjd-ordered query can be slow → generous read timeout.
    app.state.http = httpx.AsyncClient(timeout=settings.alerce_timeout_s, follow_redirects=True)
    app.state.alerce = AlerceClient(app.state.http, settings)
    app.state.requests = RequestBuilder(settings, db.sessionmaker)
    app.state.notifier = ApprovalService(settings)
    app.state.candidates = CandidateService(
        settings, observatory, db.sessionmaker,
        request_builder=app.state.requests, notifier=app.state.notifier,
    )
    app.state.notifier.bind(app.state.candidates)  # wire back-ref for Slack/email callbacks
    app.state.poller = AlertPoller(app)
    app.state.executor = ExecutionSequencer(app)
    app.state.plans = PlanService(settings, db.sessionmaker)
    app.state.phd2 = PHD2Client(settings.phd2_host or settings.indi_host, settings.phd2_port)
    app.state.auth = AuthService(settings, db.sessionmaker)
    await app.state.auth.seed_admin()
    app.state.safety = SafetyMonitor(app)
    app.state.precision = PrecisionOps(app)

    app.state.weather_api = WeatherApiPoller(app)

    await app.state.notifier.start()  # connect Slack Socket Mode (no-op without tokens)
    await app.state.phd2.start()      # connect PHD2 event server (retries if absent)
    await app.state.safety.start()    # weather + safety state machine
    await app.state.weather_api.start()  # auto-feed weather from the API
    tasks = [
        asyncio.create_task(_broadcaster(app)),
        asyncio.create_task(app.state.poller.run()),
        asyncio.create_task(app.state.executor.run()),
    ]
    log.info("CASSA core ready — INDI %s:%s, db=%s", settings.indi_host,
             settings.indi_port, settings.db_url)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await app.state.poller.stop()
        await app.state.executor.stop()
        await app.state.notifier.stop()
        await app.state.phd2.stop()
        await app.state.weather_api.stop()
        await app.state.safety.stop()
        await app.state.dm.stop()
        await app.state.http.aclose()
        await db.dispose()


app = FastAPI(title="CASSA Core", version="0.0.1", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.include_router(transient_router)

# Manual control always wins: any manual device command preempts the execution
# queue (stops auto-dispatch and pauses a running block until the operator resumes).
_MANUAL_PREFIXES = ("/api/mount/", "/api/focuser/", "/api/filter/", "/api/guide/")
_MANUAL_PATHS = {"/api/camera/expose", "/api/camera/capture", "/api/center"}


@app.middleware("http")
async def _manual_override(request, call_next):
    if request.method == "POST":
        p = request.url.path
        if p in _MANUAL_PATHS or p.startswith(_MANUAL_PREFIXES):
            ex = getattr(app.state, "executor", None)
            if ex is not None:
                ex.note_manual()
            act = getattr(app.state, "activity", None)
            if act is not None:
                act.push(p.replace("/api/", ""), "cmd")
    return await call_next(request)


# ------------------------------------------------------- RBAC middleware
_PUBLIC = {"/api/health", "/api/auth/login"}
_WRITE = {"POST", "PUT", "DELETE", "PATCH"}


def _required_role(path: str, method: str) -> str:
    if path.startswith("/api/auth/users"):
        return "admin"
    if method in _WRITE:
        # planning/curation is observer-level; running plans + device control is operator
        planning = (
            (path.startswith("/api/transient/plans") and not path.endswith("/run"))
            or path.startswith("/api/transient/candidates")
            or path == "/api/transient/poll"
        )
        return "observer" if planning else "operator"
    return "viewer"  # reads


@app.middleware("http")
async def _rbac(request: Request, call_next):
    p = request.url.path
    if request.method == "OPTIONS" or p in _PUBLIC or not p.startswith("/api/"):
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth[:7].lower() == "bearer " else request.query_params.get("token")
    try:
        user = decode_token(token or "", app.state.settings.auth_secret)
    except Exception:
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    need = _required_role(p, request.method)
    if role_rank(user.get("role")) < role_rank(need):
        return JSONResponse({"detail": f"requires '{need}' role or higher"}, status_code=403)
    request.state.user = user
    return await call_next(request)


# ------------------------------------------------------------- auth API
class LoginReq(BaseModel):
    username: str
    password: str


class UserReq(BaseModel):
    username: str
    password: str
    role: str = Field(default="viewer", pattern="^(viewer|observer|operator|admin)$")


class PasswordReq(BaseModel):
    password: str


@app.post("/api/auth/login")
async def login(req: LoginReq):
    user = await app.state.auth.authenticate(req.username, req.password)
    if not user:
        raise HTTPException(401, "invalid username or password")
    token = make_token(user["username"], user["role"], app.state.settings.auth_secret)
    return {"token": token, "user": {"username": user["username"], "role": user["role"]}}


@app.get("/api/auth/me")
async def auth_me(request: Request):
    return request.state.user  # set by the RBAC middleware


@app.get("/api/auth/users")
async def list_users():
    return await app.state.auth.list_users()


@app.post("/api/auth/users")
async def create_user(req: UserReq):
    try:
        return await app.state.auth.create_user(req.username, req.password, req.role)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/auth/users/{uid}")
async def delete_user(uid: str):
    await app.state.auth.delete_user(uid)
    return {"ok": True}


@app.post("/api/auth/users/{uid}/password")
async def set_user_password(uid: str, req: PasswordReq):
    if not await app.state.auth.set_password(uid, req.password):
        raise HTTPException(404, "user not found")
    return {"ok": True}


# --------------------------------------------------------------------- helpers
def _mount():
    dm = app.state.dm
    if not dm.connected or dm.mount is None:
        raise HTTPException(503, "mount not connected")
    return dm.mount


def _camera():
    dm = app.state.dm
    if not dm.connected or dm.camera is None:
        raise HTTPException(503, "camera not connected")
    return dm.camera


def _guider():
    dm = app.state.dm
    if not dm.connected or dm.guider is None:
        raise HTTPException(503, "guide camera not connected")
    return dm.guider


def _focuser():
    dm = app.state.dm
    if not dm.connected or dm.focuser is None:
        raise HTTPException(503, "focuser not connected")
    return dm.focuser


def _filterwheel():
    dm = app.state.dm
    if not dm.connected or dm.filterwheel is None:
        raise HTTPException(503, "filter wheel not connected")
    return dm.filterwheel


# ----------------------------------------------------------------------- core
@app.get("/api/health")
async def health():
    return {"status": "ok", "version": app.version}


@app.get("/api/status")
async def status():
    return app.state.dm.snapshot()


@app.get("/api/observatory")
async def observatory():
    """The loaded observatory definition (identity, location, equipment)."""
    return app.state.observatory.model_dump()


@app.get("/api/site")
async def site():
    """This site's summary for the central locations dashboard: identity, location,
    status, weather, and its telescopes (each with INDI endpoint + live status)."""
    obs = app.state.observatory
    snap = app.state.dm.snapshot()
    server = snap.get("server", {}) or {}
    tels = obs.telescopes_view()
    for t in tels:
        active = (t["indi_host"] == server.get("host")
                  and int(t["indi_port"]) == int(server.get("port", 0) or 0))
        t["status"] = ("online" if snap.get("indi_connected") else "standby") if active else "standby"
    # weather shown on the dashboard = the safety monitor's LIVE readings only (weather
    # API / INDI device). No static fallback — if there's no live data the card shows
    # nothing rather than a misleading default. `seeing` is the one manual value (no
    # live source — needs a DIMM); pulled from config if set.
    cfg_w = obs.weather.model_dump() if obs.weather else {}
    live = (app.state.safety.snapshot().get("weather") or {}) if hasattr(app.state, "safety") else {}
    weather = {
        "condition": live.get("condition"),
        "humidity": live.get("humidity"),
        "temperature": live.get("temperature"),
        "seeing": cfg_w.get("seeing"),
        "source": live.get("source"),
    }
    safety = app.state.safety.snapshot() if hasattr(app.state, "safety") else None
    return {
        "id": obs.id,
        "name": obs.name,
        "location": obs.location.model_dump(),
        "status": obs.status,
        "weather": weather if any(v is not None for v in weather.values()) else None,
        "safety": safety["state"] if safety else None,
        "telescopes": tels,
        "indi_connected": bool(snap.get("indi_connected")),
    }


@app.get("/api/activity")
async def activity(limit: int = 100):
    """Recent operator/system events for the console panel (newest first)."""
    act = getattr(app.state, "activity", None)
    return act.recent(min(max(limit, 1), 300)) if act else []


# ------------------------------------------------------------- safety
class WeatherReq(BaseModel):
    humidity: Optional[float] = None
    wind_speed: Optional[float] = None
    temperature: Optional[float] = None
    clouds: Optional[float] = None
    rain: Optional[bool] = None
    condition: Optional[str] = None
    seeing: Optional[str] = None
    source: str = "manual"


class ToggleReq(BaseModel):
    on: bool = False


@app.get("/api/safety")
async def safety_state():
    return app.state.safety.snapshot()


@app.post("/api/safety/weather")
async def safety_weather(req: WeatherReq):
    """Push weather readings from any source (sensor script, weather API)."""
    app.state.safety.set_weather(req.model_dump(exclude_none=True))
    return {"ok": True}


@app.post("/api/safety/estop")
async def safety_estop():
    """Emergency stop — latches FAULT, aborts the sequence and parks the mount."""
    app.state.safety.estop_trip()
    return {"ok": True}


@app.post("/api/safety/clear")
async def safety_clear():
    app.state.safety.estop_clear()
    return {"ok": True}


@app.post("/api/safety/override")
async def safety_override(req: ToggleReq):
    """Disable/enable safety enforcement (dangerous — only when attended)."""
    app.state.safety.set_override(req.on)
    return {"ok": True, "override": req.on}


# CDS Sesame resolves a name across Simbad → NED → VizieR; -oI returns ICRS deg on
# a "%J <ra> <dec>" line. Two mirrors so a blocked/down one doesn't fail the lookup.
_SESAME = (
    "https://cds.unistra.fr/cgi-bin/nph-sesame/-oI/SNV?",
    "https://vizier.cfa.harvard.edu/viz-bin/nph-sesame/-oI/SNV?",
)


_RESOLVE_CACHE: dict[str, dict] = {}

# moving solar-system bodies — computed for "now" + the site (not cached, not in catalog)
_SOLAR = {"sun", "moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune"}


def _site_earthloc(location):
    import astropy.units as u
    from astropy.coordinates import EarthLocation
    return EarthLocation(lat=float(location.latitude_deg) * u.deg,
                         lon=float(location.longitude_deg) * u.deg,
                         height=float(getattr(location, "elevation_m", 0.0) or 0.0) * u.m)


def _solar_position(body: str, location) -> dict:
    from astropy.coordinates import get_body
    from astropy.time import Time
    icrs = get_body(body, Time.now(), _site_earthloc(location)).icrs
    ra, dec = float(icrs.ra.deg), float(icrs.dec.deg)
    return {"name": body.capitalize(), "ra_deg": ra, "dec_deg": dec, "ra_hours": ra / 15.0,
            "moving": True}


def _compute_bodies(location) -> dict:
    """Current RA/Dec of the Sun, Moon, and planets (same astropy source as the
    name resolver, so the sky-map markers and a looked-up target's circle agree)."""
    from astropy.coordinates import get_body
    from astropy.time import Time
    loc = _site_earthloc(location)
    t = Time.now()
    out = {}
    for b in sorted(_SOLAR):
        try:
            icrs = get_body(b, t, loc).icrs
            out[b] = {"ra_deg": float(icrs.ra.deg), "dec_deg": float(icrs.dec.deg)}
        except Exception:
            pass
    return out


_BODIES_CACHE = {"t": 0.0, "data": None}


@app.get("/api/sky/bodies")
async def sky_bodies():
    """Sun/Moon/planet positions for the sky map (cached ~30 s)."""
    now = time.time()
    if _BODIES_CACHE["data"] is not None and now - _BODIES_CACHE["t"] < 30:
        return _BODIES_CACHE["data"]
    data = await asyncio.to_thread(_compute_bodies, app.state.observatory.location)
    _BODIES_CACHE.update(t=now, data=data)
    return data


@app.get("/api/resolve")
async def resolve(name: str):
    """Resolve an object name to RA/Dec. The Sesame mirrors are queried CONCURRENTLY
    with a short timeout (first success wins) so a slow/unreachable mirror can't stall
    the lookup; astropy is a capped fallback. Results are cached in-process."""
    from urllib.parse import quote
    from .catalog import get_catalog
    nm = name.strip()
    if not nm:
        raise HTTPException(400, "empty name")
    key = nm.lower()

    # moving solar-system bodies — computed live for now + the site (never cached)
    if key in _SOLAR:
        try:
            return await asyncio.to_thread(_solar_position, key, app.state.observatory.location)
        except Exception:
            raise HTTPException(404, f"could not compute position of '{nm}'")

    if key in _RESOLVE_CACHE:
        return _RESOLVE_CACHE[key]

    # local catalog first — instant + offline for Messier/NGC/IC/common names
    hit = get_catalog().lookup(nm)
    if hit:
        _RESOLVE_CACHE[key] = hit
        return hit

    async def _try(base: str):
        try:
            r = await app.state.http.get(base + quote(nm), timeout=8.0)
            if r.status_code == 200:
                for line in r.text.splitlines():
                    if line.startswith("%J "):
                        p = line.split()
                        ra = float(p[1])
                        return {"name": nm, "ra_deg": ra, "dec_deg": float(p[2]),
                                "ra_hours": ra / 15.0}
        except Exception:
            return None
        return None

    tasks = [asyncio.create_task(_try(b)) for b in _SESAME]
    result = None
    try:
        for fut in asyncio.as_completed(tasks):
            res = await fut
            if res:
                result = res
                break
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()

    if result is None:  # capped astropy fallback (its own Sesame client)
        try:
            from astropy.coordinates import SkyCoord
            c = await asyncio.wait_for(asyncio.to_thread(SkyCoord.from_name, nm), timeout=10.0)
            result = {"name": nm, "ra_deg": float(c.ra.deg), "dec_deg": float(c.dec.deg),
                      "ra_hours": float(c.ra.hour)}
        except Exception:
            result = None

    if result is None:
        raise HTTPException(404, f"could not resolve '{nm}'")
    _RESOLVE_CACHE[key] = result
    return result


# ---------------------------------------------------------- connection mgmt
@app.get("/api/indi/devices")
async def indi_devices():
    return app.state.dm.list_devices()


@app.post("/api/indi/rescan")
async def indi_rescan():
    """Re-send getProperties so any newly-started driver re-advertises its devices.
    The live list streams over telemetry; this just nudges a stale enumeration."""
    if not app.state.dm.connected:
        raise HTTPException(503, "INDI server not connected")
    await app.state.dm.client.get_properties()
    return {"ok": True}


@app.post("/api/indi/server")
async def indi_server(req: ServerReq):
    await app.state.dm.set_server(req.host, req.port)
    return {"ok": True, "host": req.host, "port": req.port}


@app.post("/api/devices/bind")
async def bind_device(req: BindReq):
    try:
        return await app.state.dm.bind(req.role, req.device, req.params)
    except (TimeoutError, RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/devices/unbind")
async def unbind_device(req: UnbindReq):
    await app.state.dm.unbind(req.role)
    return {"ok": True}


@app.post("/api/devices/autodetect")
async def autodetect_devices():
    if not app.state.dm.connected:
        raise HTTPException(503, "INDI server not connected")
    return {"bound": await app.state.dm.autodetect()}


# ---------------------------------------------------------------------- mount
@app.post("/api/mount/slew")
async def slew(req: SlewReq):
    await _mount().slew_to_radec(req.ra_hours, req.dec_deg, req.track)
    return {"ok": True}


@app.post("/api/mount/sync")
async def sync(req: SlewReq):
    await _mount().sync_to_radec(req.ra_hours, req.dec_deg)
    return {"ok": True}


@app.post("/api/mount/track")
async def track(req: TrackReq):
    await _mount().set_tracking(req.on)
    return {"ok": True}


@app.post("/api/mount/home")
async def home():
    await _mount().go_home()
    return {"ok": True}


@app.post("/api/mount/set-home")
async def set_home():
    await _mount().set_home()
    return {"ok": True}


@app.post("/api/mount/set-park")
async def set_park():
    await _mount().set_park()
    return {"ok": True}


@app.post("/api/mount/abort")
async def abort():
    await _mount().abort()
    return {"ok": True}


@app.post("/api/mount/park")
async def park():
    await _mount().park(True)
    return {"ok": True}


@app.post("/api/mount/unpark")
async def unpark():
    await _mount().park(False)
    return {"ok": True}


# --------------------------------------------------------------------- focuser
@app.post("/api/focuser/move")
async def focuser_move(req: FocusAbsReq):
    await _focuser().move_absolute(req.position)
    return {"ok": True}


@app.post("/api/focuser/rel")
async def focuser_rel(req: FocusRelReq):
    await _focuser().move_relative(req.steps, req.inward)
    return {"ok": True}


@app.post("/api/focuser/autofocus")
async def focuser_autofocus():
    """Run an HFR V-curve autofocus sweep (background; progress in telemetry)."""
    if app.state.precision.busy():
        raise HTTPException(409, "a precision op is already running")
    app.state.precision.start_autofocus()
    return {"started": True}


# ----------------------------------------------------------- precision pointing
class CenterReq(BaseModel):
    ra_hours: float
    dec_deg: float


@app.post("/api/center")
async def center(req: CenterReq):
    """Plate-solve and center the target (capture→solve→sync→re-slew; background)."""
    if app.state.precision.busy():
        raise HTTPException(409, "a precision op is already running")
    app.state.precision.start_center(req.ra_hours, req.dec_deg)
    return {"started": True}


@app.get("/api/precision")
async def precision_state():
    return app.state.precision.snapshot()


# ---------------------------------------------------------------------- filter
@app.post("/api/filter/set")
async def filter_set(req: FilterReq):
    await _filterwheel().set_position(req.slot)
    return {"ok": True}


@app.post("/api/filter/name")
async def filter_name(req: FilterNameReq):
    await _filterwheel().set_name(req.slot, req.name.strip())
    return {"ok": True}


# ---------------------------------------------------------------------- camera
@app.post("/api/camera/expose")
async def expose(req: ExposeReq):
    await _camera().expose(req.seconds)
    return {"ok": True}


@app.post("/api/camera/capture")
async def capture(req: CaptureReq):
    if not app.state.dm.connected:
        raise HTTPException(503, "devices not connected")
    authored = await app.state.dm.capture(
        req.seconds, req.image_type, req.object_name, req.filter_slot
    )
    return await app.state.archive.ingest(authored["fits"], authored["meta"])


@app.get("/api/camera/last-image.png")
async def last_image():
    png = app.state.dm.latest_png
    if not png:
        raise HTTPException(404, "no image yet")
    return Response(content=png, media_type="image/png")


# ----------------------------------------------------------------- guide camera
@app.post("/api/guide/expose")
async def guide_expose(req: ExposeReq):
    await _guider().expose(req.seconds)
    return {"ok": True}


@app.post("/api/guide/capture")
async def guide_capture(req: CaptureReq):
    if not app.state.dm.connected:
        raise HTTPException(503, "devices not connected")
    authored = await app.state.dm.capture(
        req.seconds, req.image_type, req.object_name, req.filter_slot, role="guide"
    )
    return await app.state.archive.ingest(authored["fits"], authored["meta"])


@app.get("/api/guide/last-image.png")
async def last_guide_image():
    png = app.state.dm.latest_guide_png
    if not png:
        raise HTTPException(404, "no image yet")
    return Response(content=png, media_type="image/png")


# ------------------------------------------------------------- PHD2 guiding
@app.get("/api/guiding/graph")
async def guiding_graph(limit: int = 200):
    """Recent guiding error samples (RA/Dec) + state/RMS for the guiding plot."""
    return app.state.phd2.graph(min(max(limit, 1), 400))


@app.post("/api/guiding/start")
async def guiding_start():
    try:
        await app.state.phd2.guide()
    except Exception as e:
        raise HTTPException(503, f"PHD2: {e}")
    return {"ok": True}


@app.post("/api/guiding/stop")
async def guiding_stop():
    try:
        await app.state.phd2.stop_guiding()
    except Exception as e:
        raise HTTPException(503, f"PHD2: {e}")
    return {"ok": True}


# --------------------------------------------------------------------- archive
@app.get("/api/images")
async def list_images(limit: int = 50):
    return await app.state.archive.list_images(min(max(limit, 1), 500))


@app.get("/api/images/{image_id}")
async def image_meta(image_id: str):
    obj = await app.state.archive.get(image_id)
    if not obj:
        raise HTTPException(404, "image not found")
    return obj.dict()


@app.get("/api/images/{image_id}/fits")
async def image_fits(image_id: str):
    obj = await app.state.archive.get(image_id)
    if not obj:
        raise HTTPException(404, "image not found")
    data = app.state.store.get(obj.fits_key)
    return Response(
        content=data,
        media_type="application/fits",
        headers={"Content-Disposition": f'attachment; filename="{obj.obsid}.fits"'},
    )


@app.get("/api/images/{image_id}/preview.png")
async def image_preview(image_id: str):
    obj = await app.state.archive.get(image_id)
    if not obj or not obj.preview_key:
        raise HTTPException(404, "no preview")
    return Response(content=app.state.store.get(obj.preview_key), media_type="image/png")


@app.get("/api/images/{image_id}/thumb.png")
async def image_thumb(image_id: str):
    obj = await app.state.archive.get(image_id)
    if not obj or not obj.thumb_key:
        raise HTTPException(404, "no thumbnail")
    return Response(content=app.state.store.get(obj.thumb_key), media_type="image/png")


# ------------------------------------------------------------------- telemetry
@app.websocket("/ws/telemetry")
async def telemetry(ws: WebSocket):
    try:
        decode_token(ws.query_params.get("token") or "", app.state.settings.auth_secret)
    except Exception:
        await ws.close(code=1008)  # policy violation — not authenticated
        return
    await ws.accept()
    app.state.clients.add(ws)
    try:
        await ws.send_json(app.state.dm.snapshot())
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        app.state.clients.discard(ws)
