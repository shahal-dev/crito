"""REST surface for the transient follow-up pipeline.

Mounted on the core app via ``app.include_router``. Reads services off
``request.app.state`` (alerce, candidates, poller, db) wired in the lifespan.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from ..transient.approvals import verify_token
from .transient_db import Alert

router = APIRouter(prefix="/api/transient", tags=["transient"])


class ApproveReq(BaseModel):
    action: str = Field(pattern="^(queue|execute)$")
    recipe: list | None = None
    scheduled_utc: str | None = None   # optional start time (ISO UTC); queue runs at this time
    launch: bool = False               # execute now: launch the block immediately
    actor: str = "console"


class RejectReq(BaseModel):
    actor: str = "console"


class ReorderReq(BaseModel):
    block_ids: list[str]


class LaunchReq(BaseModel):
    block_id: str


class OverrideReq(BaseModel):
    on: bool = True


class PlanReq(BaseModel):
    id: str | None = None
    name: str = "Untitled plan"
    object_name: str = ""
    ra_deg: float | None = None
    dec_deg: float | None = None
    recipe: list = []  # [{filter_slot, filter_name, exptime_s, count, binning, dither_px}]
    repeat: int = Field(default=1, ge=1)
    autofocus: bool = False
    center: bool = False
    scheduled_utc: str | None = None   # ISO UTC; auto-run at this time (None = run manually)
    source: str | None = "manual"


# ---------------------------------------------------------------- night / poll
@router.get("/night")
async def night(request: Request):
    svc = request.app.state.candidates
    return svc.night().info()


@router.post("/poll")
async def poll_now(request: Request):
    """Trigger an ALeRCE poll immediately (testability)."""
    return await request.app.state.poller.poll_once()


@router.get("/alerce/raw")
async def alerce_raw(request: Request):
    """Diagnostic: one live ALeRCE query, returned raw, on the backend's network.
    Use this to see exactly what the broker returns when no candidates appear."""
    return await request.app.state.alerce.probe()


# ------------------------------------------------------------------- alerts
@router.get("/alerts")
async def list_alerts(request: Request, limit: int = 50):
    sm = request.app.state.db.sessionmaker
    async with sm() as session:
        rows = (await session.execute(
            select(Alert).order_by(desc(Alert.lastmjd)).limit(min(max(limit, 1), 500))
        )).scalars().all()
        return [r.dict() for r in rows]


# --------------------------------------------------------------- candidates
@router.get("/candidates")
async def list_candidates(request: Request, ut_date: str | None = None,
                          state: str | None = None, group_by: str | None = None):
    return await request.app.state.candidates.list_candidates(ut_date, state, group_by)


@router.get("/candidates/{cand_id}")
async def get_candidate(request: Request, cand_id: str):
    obj = await request.app.state.candidates.get(cand_id)
    if not obj:
        raise HTTPException(404, "candidate not found")
    return obj


@router.post("/candidates/{cand_id}/approve")
async def approve_candidate(request: Request, cand_id: str, req: ApproveReq):
    try:
        result = await request.app.state.candidates.approve(
            cand_id, req.action, req.actor, req.recipe, scheduled_utc=req.scheduled_utc
        )
    except KeyError:
        raise HTTPException(404, "candidate not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    # "execute now" → launch the created block immediately
    if req.launch and result.get("block_id"):
        request.app.state.executor.launch(result["block_id"])
    return result


@router.post("/candidates/{cand_id}/reset")
async def reset_candidate(request: Request, cand_id: str, req: RejectReq):
    """Re-open a decided candidate so its Queue/Execute buttons work again."""
    try:
        return await request.app.state.candidates.reset(cand_id, req.actor)
    except KeyError:
        raise HTTPException(404, "candidate not found")


@router.post("/candidates/{cand_id}/reject")
async def reject_candidate(request: Request, cand_id: str, req: RejectReq):
    try:
        return await request.app.state.candidates.reject(cand_id, req.actor)
    except KeyError:
        raise HTTPException(404, "candidate not found")


@router.get("/approve", response_class=HTMLResponse)
async def approve_via_email(request: Request, token: str):
    """Signed one-click approval from an email deep-link (HMAC-verified)."""
    secret = request.app.state.settings.approve_secret
    try:
        cid, action = verify_token(token, secret)
    except ValueError as e:
        raise HTTPException(400, f"invalid link: {e}")
    svc = request.app.state.candidates
    try:
        if action == "reject":
            await svc.reject(cid, actor="email")
        else:
            await svc.approve(cid, action, actor="email")
    except KeyError:
        raise HTTPException(404, "candidate not found")
    verb = "rejected" if action == "reject" else f"approved → {action}"
    return (f"<html><body style='font-family:sans-serif;background:#0b0e14;color:#c9d4e3;"
            f"padding:40px'><h2>CRITO</h2><p>Candidate <b>{cid}</b> {verb}.</p>"
            f"<p>You can close this tab.</p></body></html>")


# ------------------------------------------------------ requests / queue
@router.get("/requests")
async def list_requests(request: Request, limit: int = 100):
    return await request.app.state.requests.list_requests(limit)


@router.get("/queue")
async def list_queue(request: Request):
    return await request.app.state.requests.list_queue()


@router.post("/queue/reorder")
async def reorder_queue(request: Request, req: ReorderReq):
    return await request.app.state.requests.reorder_queue(req.block_ids)


@router.delete("/queue/{block_id}")
async def cancel_queue_block(request: Request, block_id: str):
    """Remove a block from the queue (abort it if it's currently running)."""
    try:
        res = await request.app.state.requests.cancel_block(block_id)
    except KeyError:
        raise HTTPException(404, "block not found")
    await request.app.state.executor.cancel(block_id)
    return {"ok": True, **res}


# ----------------------------------------------------------------- executor
@router.get("/executor")
async def executor_state(request: Request):
    return request.app.state.executor.snapshot()


@router.post("/executor/launch")
async def executor_launch(request: Request, req: LaunchReq):
    request.app.state.executor.launch(req.block_id)
    return {"ok": True, "launched": req.block_id}


@router.post("/executor/pause")
async def executor_pause(request: Request):
    request.app.state.executor.pause()
    return {"ok": True}


@router.post("/executor/resume")
async def executor_resume(request: Request):
    request.app.state.executor.resume()
    return {"ok": True}


@router.post("/executor/abort")
async def executor_abort(request: Request):
    await request.app.state.executor.abort()
    return {"ok": True}


@router.post("/executor/override")
async def executor_override(request: Request, req: OverrideReq):
    request.app.state.executor.set_override(req.on)
    return {"ok": True, "manual_override": req.on}


@router.post("/executor/confirm")
async def executor_confirm(request: Request):
    """Operator confirms a prompt step (e.g. flat-frame setup) — resume the block."""
    request.app.state.executor.confirm()
    return {"ok": True}


# ------------------------------------------------------------- plans
@router.get("/plans")
async def list_plans(request: Request):
    return await request.app.state.plans.list_plans()


@router.get("/plans/{pid}")
async def get_plan(request: Request, pid: str):
    p = await request.app.state.plans.get_plan(pid)
    if not p:
        raise HTTPException(404, "plan not found")
    return p


@router.post("/plans")
async def save_plan(request: Request, req: PlanReq):
    return await request.app.state.plans.save_plan(req.model_dump())


@router.delete("/plans/{pid}")
async def delete_plan(request: Request, pid: str):
    await request.app.state.plans.delete_plan(pid)
    return {"ok": True}


@router.post("/plans/{pid}/run")
async def run_plan(request: Request, pid: str, resume: bool = False):
    """Expand the plan into an execution block and launch it. resume=true continues
    the plan's last incomplete block (already-done steps are skipped)."""
    try:
        res = await request.app.state.plans.run_plan(pid, resume=resume)
    except KeyError:
        raise HTTPException(404, "plan not found")
    request.app.state.executor.launch(res["block_id"])
    return res
