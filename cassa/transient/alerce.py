"""ALeRCE broker client — async REST polling of the ZTF object list.

Uses the public ALeRCE ZTF API (https://api.alerce.online/ztf/v1) over httpx — no
Kafka, no pandas ``alerce`` client. Objects are normalized to the dict shape the
``Alert`` model wants; the full raw item is preserved so a change in the broker's
schema is debuggable and reprocessable.

Field names are read defensively (``.get`` with fallbacks) because the list
endpoint's exact keys vary by classifier/version; the raw item is always kept.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

log = logging.getLogger("cassa.transient.alerce")

_BASE = "https://api.alerce.online/ztf/v1"


def _now_mjd() -> float:
    return time.time() / 86400.0 + 40587.0


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def normalize_object(item: dict) -> dict:
    """Map a raw ALeRCE object to the canonical Alert dict. Missing fields become
    None — never raises."""
    ndet = item.get("ndethist")
    if ndet is None:
        ndet = item.get("ndet")
    return {
        "id": item.get("oid"),
        "source": "alerce",
        "ra_deg": _to_float(item.get("meanra")),
        "dec_deg": _to_float(item.get("meandec")),
        # class label carries through whichever key the classifier filter populated
        "class_label": item.get("class_name") or item.get("class") or item.get("classearly"),
        "class_prob": _to_float(item.get("probability")),
        "mag_last": _to_float(item.get("lastmag") or item.get("magpsf")),
        "ndethist": _to_int(ndet),
        "firstmjd": _to_float(item.get("firstmjd")),
        "lastmjd": _to_float(item.get("lastmjd")),
        "raw_json": item,
    }


class AlerceClient:
    """Queries the ALeRCE ZTF object API (``GET /ztf/v1/objects``, list under
    ``items``). Default strategy = the **stamp classifier** per transient class so
    each object comes back with a real class label + probability; if that yields
    nothing it falls back to a plain recent-objects query so the feed is never
    empty. (There is no ``ranking`` parameter — the valid params are ``classifier``,
    ``class``, ``probability``, ``ndet``, ``firstmjd``/``lastmjd``, ``order_by``,
    ``order_mode``, ``page``, ``page_size``, ``count``.)"""

    def __init__(self, http: httpx.AsyncClient, settings):
        self.http = http
        self.s = settings
        self.last_error: str | None = None   # surfaced via /api/transient/poll

    def _classes(self) -> list[str]:
        return [c.strip() for c in (getattr(self.s, "alerce_classes", "") or "").split(",") if c.strip()]

    def _params(self, classifier: str | None, cls: str | None) -> dict:
        params = {
            "order_by": "lastmjd",
            "order_mode": "DESC",
            "count": "false",
            "page_size": self.s.alerce_page_size,
        }
        if classifier:
            params["classifier"] = classifier
            if cls:
                params["class"] = cls
            prob = getattr(self.s, "alerce_probability", 0.0)
            if prob:
                params["probability"] = prob
        if self.s.alerce_min_ndet:
            params["ndet"] = self.s.alerce_min_ndet
        return params

    @staticmethod
    def _extract_items(payload) -> list:
        if isinstance(payload, dict):
            for key in ("items", "results", "data", "objects"):
                v = payload.get(key)
                if isinstance(v, list):
                    return v
            return []
        return payload if isinstance(payload, list) else []

    async def _collect(self, cutoff_mjd, classifier=None, cls=None) -> dict:
        """Run one (classifier, class) query and return its objects keyed by oid.
        A server-side ``lastmjd`` lower bound keeps the broker from sorting its whole
        table (the cause of read timeouts) — only recently-active objects are scanned."""
        params = self._params(classifier, cls)
        if cutoff_mjd is not None:
            params["lastmjd"] = [cutoff_mjd, cutoff_mjd + 100000.0]  # [min, max] range
        out: dict[str, dict] = {}
        for page in range(1, self.s.alerce_max_pages + 1):
            params["page"] = page
            try:
                r = await self.http.get(f"{_BASE}/objects/", params=params)
                r.raise_for_status()
                payload = r.json()
            except httpx.HTTPStatusError as e:
                self.last_error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
                log.warning("ALeRCE %s (classifier=%s class=%s)", self.last_error, classifier, cls)
                return out
            except (httpx.HTTPError, ValueError) as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("ALeRCE request failed: %s", self.last_error)
                return out
            items = self._extract_items(payload)
            if not items:
                if page == 1 and isinstance(payload, dict):
                    self.last_error = self.last_error or f"no items (keys: {list(payload.keys())})"
                return out
            for item in items:
                obj = normalize_object(item)
                if not obj["id"]:
                    continue
                if (cutoff_mjd is not None and obj["lastmjd"] is not None
                        and obj["lastmjd"] < cutoff_mjd):
                    continue   # too old — skip, keep scanning
                if cls and not obj["class_label"]:
                    obj["class_label"] = cls          # ensure a label even if absent in row
                out[obj["id"]] = obj
            if len(items) < self.s.alerce_page_size:
                return out        # last page
        return out

    @staticmethod
    def _merge(dst: dict, src: dict) -> None:
        for oid, obj in src.items():
            prev = dst.get(oid)
            if prev is None or (obj["class_prob"] or 0) > (prev["class_prob"] or 0):
                dst[oid] = obj

    async def query_recent(self, cutoff_mjd: float | None = None) -> list[dict]:
        """Recent objects, normalized + de-duplicated by oid. Queries the configured
        classifier per class **concurrently**; falls back to a plain recent query if
        that's empty so the feed is never silently empty."""
        self.last_error = None
        classifier, classes = self.s.alerce_classifier, self._classes()
        if classifier and classes:
            dicts = await asyncio.gather(*[self._collect(cutoff_mjd, classifier, c) for c in classes])
        elif classifier:
            dicts = [await self._collect(cutoff_mjd, classifier)]
        else:
            dicts = [await self._collect(cutoff_mjd)]
        out: dict[str, dict] = {}
        for d in dicts:
            self._merge(out, d)
        if not out and classifier:
            log.warning("ALeRCE classified query empty — falling back to recent objects")
            self._merge(out, await self._collect(cutoff_mjd))   # no classifier
        objs = list(out.values())
        log.info("ALeRCE: %d recent object(s) (cutoff_mjd=%s, classifier=%s)",
                 len(objs), cutoff_mjd, classifier or "none")
        return objs

    async def probe(self) -> dict:
        """One live query for diagnostics (GET /api/transient/alerce/raw): real
        status, final URL, envelope keys, item count, and the first object."""
        classes = self._classes()
        params = self._params(self.s.alerce_classifier, classes[0] if classes else None)
        cutoff = _now_mjd() - float(getattr(self.s, "alerce_lookback_days", 7.0))
        params["lastmjd"] = [cutoff, cutoff + 100000.0]
        params.update(page=1, page_size=3)
        try:
            r = await self.http.get(f"{_BASE}/objects/", params=params)
            try:
                payload = r.json()
                items = self._extract_items(payload)
                return {
                    "status": r.status_code,
                    "final_url": str(r.url),
                    "envelope_keys": list(payload.keys()) if isinstance(payload, dict) else "list",
                    "item_count": len(items),
                    "first_item": items[0] if items else None,
                    "first_normalized": normalize_object(items[0]) if items else None,
                }
            except ValueError:
                return {"status": r.status_code, "final_url": str(r.url),
                        "error": "response was not JSON", "body_head": r.text[:400]}
        except httpx.HTTPError as e:
            return {"error": f"{type(e).__name__}: {e}"}
