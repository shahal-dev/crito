"""AlertPoller — background task that polls ALeRCE, upserts Alert rows, and hands
new/updated objects to the CandidateService for visibility evaluation.

Started in ``app.py`` lifespan next to ``_broadcaster``. Defensive per-cycle: a
broker hiccup logs and retries, never kills the task (same style as
``DeviceManager._run``). A high-water ``lastmjd`` keeps each poll to the deltas.
"""
from __future__ import annotations

import asyncio
import logging

from ..core.db import _utcnow
from ..core.transient_db import Alert
from .visibility import now_mjd

log = logging.getLogger("crito.transient.poller")


class AlertPoller:
    def __init__(self, app):
        self.app = app
        self.s = app.state.settings
        self._stop = False

    async def run(self) -> None:
        # small initial delay so the device transport settles first
        await asyncio.sleep(3.0)
        while not self._stop:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ALeRCE poll cycle failed")
            await asyncio.sleep(max(30, self.s.alerce_poll_s))

    async def stop(self) -> None:
        self._stop = True

    async def poll_once(self) -> dict:
        cutoff = now_mjd() - float(self.s.alerce_lookback_days)
        alerce = self.app.state.alerce
        objs = await alerce.query_recent(cutoff)
        await self._upsert_alerts(objs)
        observable = 0
        if getattr(self.app.state, "candidates", None):
            observable = await self.app.state.candidates.evaluate_alerts(objs)
        act = getattr(self.app.state, "activity", None)
        if act is not None:
            act.push(f"ALeRCE poll: {len(objs)} fetched, {observable} observable"
                     + (f" — {alerce.last_error}" if alerce.last_error else ""),
                     "error" if alerce.last_error else "info")
        # `fetched` localizes the problem: 0 → ingest/query issue (see `error`);
        # >0 with observable 0 → nothing clears the horizon tonight.
        return {"fetched": len(objs), "observable": observable, "error": alerce.last_error}

    async def _upsert_alerts(self, objs: list[dict]) -> None:
        if not objs:
            return
        sm = self.app.state.db.sessionmaker
        async with sm() as session:
            for obj in objs:
                a = await session.get(Alert, obj["id"])
                if a is None:
                    a = Alert(id=obj["id"], source=obj["source"])
                    session.add(a)
                a.last_seen_utc = _utcnow()
                a.ra_deg = obj.get("ra_deg")
                a.dec_deg = obj.get("dec_deg")
                a.class_label = obj.get("class_label")
                a.class_prob = obj.get("class_prob")
                a.mag_last = obj.get("mag_last")
                a.ndethist = obj.get("ndethist")
                a.firstmjd = obj.get("firstmjd")
                a.lastmjd = obj.get("lastmjd")
                a.raw_json = obj.get("raw_json")
            await session.commit()
