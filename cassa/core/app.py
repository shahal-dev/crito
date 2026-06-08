"""CASSA Core API (Phase 1).

Manual control (mount, camera, focuser, filter wheel), full-frame capture with FITS
authoring + archive ingest, an image archive (search/download/preview), and a live
telemetry WebSocket. Run with:  uvicorn cassa.core.app:app --reload
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..agent.device_manager import DeviceManager
from .archive import ArchiveService
from .config import load_settings
from .db import DB
from .storage import LocalStore

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("cassa.core")

_TELEMETRY_HZ = 2.0


class SlewReq(BaseModel):
    ra_hours: float = Field(ge=0, lt=24)
    dec_deg: float = Field(ge=-90, le=90)
    track: bool = True


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


async def _broadcaster(app: FastAPI) -> None:
    period = 1.0 / _TELEMETRY_HZ
    while True:
        await asyncio.sleep(period)
        clients = app.state.clients
        if not clients:
            continue
        snap = app.state.dm.snapshot()
        for ws in list(clients):
            try:
                await ws.send_json(snap)
            except Exception:
                clients.discard(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    pathlib.Path("data").mkdir(exist_ok=True)

    store = LocalStore(settings.data_dir)
    db = DB(settings.db_url)
    await db.init()
    app.state.settings = settings
    app.state.store = store
    app.state.db = db
    app.state.archive = ArchiveService(store, db.sessionmaker)
    app.state.dm = DeviceManager(settings)
    app.state.clients = set()

    await app.state.dm.start()
    task = asyncio.create_task(_broadcaster(app))
    log.info("CASSA core ready — INDI %s:%s, db=%s", settings.indi_host,
             settings.indi_port, settings.db_url)
    try:
        yield
    finally:
        task.cancel()
        await app.state.dm.stop()
        await db.dispose()


app = FastAPI(title="CASSA Core", version="0.0.1", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


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


# ---------------------------------------------------------------------- mount
@app.post("/api/mount/slew")
async def slew(req: SlewReq):
    await _mount().slew_to_radec(req.ra_hours, req.dec_deg, req.track)
    return {"ok": True}


@app.post("/api/mount/sync")
async def sync(req: SlewReq):
    await _mount().sync_to_radec(req.ra_hours, req.dec_deg)
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


# ---------------------------------------------------------------------- filter
@app.post("/api/filter/set")
async def filter_set(req: FilterReq):
    await _filterwheel().set_position(req.slot)
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
