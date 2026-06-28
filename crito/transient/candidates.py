"""CandidateService — turn ingested alerts into visibility-filtered candidates,
score them, group by class, and drive approve/reject transitions.

Ingest → visibility (astropy) → persist Candidate (one per object per night) →
score. Approval transitions write an AuditEvent and, when a RequestBuilder is
wired (Phase E), create the observation request. Notification (Phase D) is
delegated to an injected notifier.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import select

from ..core.db import _utcnow
from ..core.transient_db import (
    Alert,
    AuditEvent,
    BlockState,
    Candidate,
    CandidateState,
    ExecutionBlock,
    ObservationRequest,
    RequestState,
)
from . import visibility as vis

log = logging.getLogger("crito.transient.candidates")


class CandidateService:
    def __init__(self, settings, observatory, sessionmaker, request_builder=None, notifier=None):
        self.s = settings
        self.obs = observatory
        self.sm = sessionmaker
        self.request_builder = request_builder
        self.notifier = notifier
        self._loc = vis.site_location(observatory.location)
        self._night_cache: dict[str, vis.NightWindow] = {}

    # --------------------------------------------------------------- night
    def night(self, when: dt.datetime | None = None) -> vis.NightWindow:
        when = when or _utcnow()
        label = vis.night_label(when, self.s.utc_offset_hours)
        nb = self._night_cache.get(label)
        if nb is None:
            nb = vis.compute_night(self._loc, when, self.s.utc_offset_hours)
            self._night_cache = {nb.ut_date: nb}  # only ever need the current night
        return nb

    # ------------------------------------------------------------- scoring
    def _score(self, class_prob, max_alt, min_airmass, moon_sep, mag) -> float:
        s = self.s
        score = 0.0
        score += s.score_w_prob * (class_prob or 0.0)
        score += s.score_w_alt * ((max_alt or 0.0) / 90.0)
        if min_airmass:
            score += s.score_w_airmass * (1.0 / min_airmass)
        score += s.score_w_moon * ((moon_sep or 0.0) / 180.0)
        if mag is not None and s.mag_limit:
            score -= s.score_w_faint * (mag / s.mag_limit)
        return round(score, 4)

    # ------------------------------------------------------------- ingest
    def _compute_all(self, objs: list[dict], night) -> list[tuple]:
        """Pure-CPU visibility for every alert with coordinates — run in a thread."""
        out = []
        for obj in objs:
            ra, dec = obj.get("ra_deg"), obj.get("dec_deg")
            if ra is None or dec is None:
                continue
            out.append((obj, vis.visibility(ra, dec, night, self._loc, self.s.alt_min_deg)))
        return out

    async def evaluate_alerts(self, objs: list[dict]) -> int:
        """Upsert a Candidate for *every* alert (with coordinates), tagging each with
        its observability. Returns the count of observable candidates. Visibility is
        computed in a worker thread so the whole feed never stalls the event loop."""
        if not objs:
            return 0
        night = self.night()
        evald = await asyncio.to_thread(self._compute_all, objs, night)
        observable = 0
        new_observable_ids: list[str] = []
        async with self.sm() as session:
            for obj, v in evald:
                if v.observable:
                    observable += 1
                cid = f"{obj['id']}_{night.ut_date}"
                mag = obj.get("mag_last")
                score = self._score(obj.get("class_prob"), v.max_alt_deg,
                                    v.min_airmass, v.moon_sep_deg, mag)
                cand = await session.get(Candidate, cid)
                is_new = cand is None
                if is_new:
                    cand = Candidate(id=cid, alert_id=obj["id"], ut_date=night.ut_date,
                                     state=CandidateState.NEW.value)
                    session.add(cand)
                # refresh visibility/score on every pass; never downgrade a decision
                cand.class_label = obj.get("class_label")
                cand.class_prob = obj.get("class_prob")
                cand.ra_deg, cand.dec_deg, cand.mag = obj["ra_deg"], obj["dec_deg"], mag
                cand.score = score
                cand.window_start_utc = v.window_start_utc
                cand.window_end_utc = v.window_end_utc
                cand.max_alt_deg = v.max_alt_deg
                cand.min_airmass = v.min_airmass
                cand.moon_sep_deg = v.moon_sep_deg
                cand.moon_illum_frac = v.moon_illum_frac
                cand.computed_at = _utcnow()
                if is_new and v.observable:          # only ping the supervisor for observable ones
                    new_observable_ids.append(cid)
            await session.commit()
        if new_observable_ids:
            log.info("%d new observable candidate(s) for night %s",
                     len(new_observable_ids), night.ut_date)
            await self._notify_new(new_observable_ids)
        return observable

    async def _notify_new(self, cand_ids: list[str]) -> None:
        if not self.notifier:
            return
        for cid in cand_ids:
            try:
                async with self.sm() as session:
                    cand = await session.get(Candidate, cid)
                    if not cand or cand.state != CandidateState.NEW.value:
                        continue
                    detail = cand.dict()
                await self.notifier.notify(detail)
                async with self.sm() as session:
                    cand = await session.get(Candidate, cid)
                    if cand and cand.state == CandidateState.NEW.value:
                        cand.state = CandidateState.NOTIFIED.value
                        cand.notified_at = _utcnow()
                        await session.commit()
            except Exception:
                log.exception("notify failed for candidate %s", cid)

    # ------------------------------------------------------------- queries
    @staticmethod
    def _tag(d: dict) -> dict:
        """Derive the observability flag from whether a window was found (no schema
        change needed — window is set iff the object clears the limit tonight)."""
        d["observable"] = d.get("window_start_utc") is not None
        return d

    async def list_candidates(self, ut_date=None, state=None, group_by=None):
        async with self.sm() as session:
            stmt = select(Candidate)
            if ut_date:
                stmt = stmt.where(Candidate.ut_date == ut_date)
            if state:
                stmt = stmt.where(Candidate.state == state)
            rows = (await session.execute(stmt)).scalars().all()
        # observable first, then by score — within each class group too
        items = sorted((self._tag(r.dict()) for r in rows),
                       key=lambda it: (it["observable"], it["score"]), reverse=True)
        if group_by == "class":
            groups: dict[str, list] = {}
            for it in items:
                groups.setdefault(it.get("class_label") or "unknown", []).append(it)
            return {"groups": groups, "count": len(items),
                    "observable": sum(1 for it in items if it["observable"])}
        return items

    async def get(self, cand_id: str) -> dict | None:
        async with self.sm() as session:
            cand = await session.get(Candidate, cand_id)
            if not cand:
                return None
            out = self._tag(cand.dict())
            audit = (await session.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "candidate", AuditEvent.entity_id == cand_id
                ).order_by(AuditEvent.ts)
            )).scalars().all()
            out["audit"] = [a.dict() for a in audit]
            return out

    # ---------------------------------------------------------- transitions
    async def _audit(self, session, actor, action, entity_type, entity_id, detail=None, result=None):
        session.add(AuditEvent(actor=actor, action=action, entity_type=entity_type,
                               entity_id=entity_id, detail_json=detail, result=result))

    async def approve(self, cand_id: str, action: str, actor: str, recipe=None,
                      scheduled_utc=None) -> dict:
        """action: 'queue' (attended) or 'execute' (run now / auto)."""
        if action not in ("queue", "execute"):
            raise ValueError(f"unknown approve action {action!r}")
        new_state = (CandidateState.APPROVED_QUEUE if action == "queue"
                     else CandidateState.APPROVED_EXECUTE).value
        async with self.sm() as session:
            cand = await session.get(Candidate, cand_id)
            if not cand:
                raise KeyError(cand_id)
            if cand.state in (CandidateState.REJECTED.value,
                              CandidateState.APPROVED_QUEUE.value,
                              CandidateState.APPROVED_EXECUTE.value):
                return cand.dict()  # idempotent — already decided
            cand.state = new_state
            cand.decided_at = _utcnow()
            cand.decided_by = actor
            await self._audit(session, actor, f"approve_{action}", "candidate", cand_id)
            await session.commit()
            cand_dict = cand.dict()
        # build the observation request + execution block (Phase E)
        if self.request_builder:
            try:
                req = await self.request_builder.build(cand_dict, action=action, recipe=recipe,
                                                       created_by=actor, scheduled_utc=scheduled_utc)
                async with self.sm() as session:
                    cand = await session.get(Candidate, cand_id)
                    cand.request_id = req["id"]
                    await session.commit()
                cand_dict["request_id"] = req["id"]
            except Exception:
                log.exception("request build failed for candidate %s", cand_id)
        return cand_dict

    async def reject(self, cand_id: str, actor: str) -> dict:
        async with self.sm() as session:
            cand = await session.get(Candidate, cand_id)
            if not cand:
                raise KeyError(cand_id)
            cand.state = CandidateState.REJECTED.value
            cand.decided_at = _utcnow()
            cand.decided_by = actor
            await self._audit(session, actor, "reject", "candidate", cand_id)
            await session.commit()
            return cand.dict()

    async def reset(self, cand_id: str, actor: str = "console") -> dict:
        """Re-open a decided candidate (approved/rejected) back to NEW so it can be
        queued/executed again; aborts any still-active block it created."""
        async with self.sm() as session:
            cand = await session.get(Candidate, cand_id)
            if not cand:
                raise KeyError(cand_id)
            old_req = cand.request_id
            cand.state = CandidateState.NEW.value
            cand.decided_at = None
            cand.decided_by = None
            cand.request_id = None
            await self._audit(session, actor, "reset", "candidate", cand_id)
            if old_req:
                blocks = (await session.execute(
                    select(ExecutionBlock).where(
                        ExecutionBlock.request_id == old_req,
                        ExecutionBlock.state.in_([BlockState.QUEUED.value, BlockState.PAUSED.value]),
                    ))).scalars().all()
                for b in blocks:
                    b.state = BlockState.ABORTED.value
                    b.ended_at = _utcnow()
                req = await session.get(ObservationRequest, old_req)
                if req:
                    req.state = RequestState.CANCELLED.value
            await session.commit()
            return cand.dict()
