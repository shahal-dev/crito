"""Local object-name catalog (OpenNGC, vendored).

Resolves common deep-sky names — Messier, NGC, IC, and common names — to RA/Dec
instantly and offline, so name lookups don't hit the network for the usual targets.
The slim catalog lives at ``cassa/data/openngc.tsv`` (regenerate from OpenNGC's
NGC.csv). Anything not in it (transients, stars, obscure names) falls back to Sesame.
"""
from __future__ import annotations

import logging
import pathlib
import re

log = logging.getLogger("cassa.catalog")

# "M 42" | "M42" | "Messier42" | "NGC 1976" | "NGC0224" | "IC1318a" → canonical key
_ID_RE = re.compile(r"^(messier|m|ngc|ic)\s*0*(\d+)\s*([a-z]?)$")
_PREFIX = {"messier": "m", "m": "m", "ngc": "ngc", "ic": "ic"}


def normalize(name: str) -> str:
    """Canonicalize a name for matching: catalog ids → prefix+int (leading zeros and
    spaces stripped); everything else → lowercase alphanumerics only."""
    s = " ".join(name.strip().lower().split())
    m = _ID_RE.match(s)
    if m:
        return _PREFIX[m.group(1)] + str(int(m.group(2))) + m.group(3)
    return re.sub(r"[^a-z0-9]+", "", s)


class Catalog:
    def __init__(self):
        self._by_alias: dict[str, tuple[float, float, str]] = {}

    @property
    def size(self) -> int:
        return len(self._by_alias)

    def load(self, path: pathlib.Path) -> None:
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            try:
                ra_d, dec_d = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            entry = (ra_d, dec_d, parts[2])
            for nm in parts[3].split("|"):
                key = normalize(nm)
                if key:
                    self._by_alias.setdefault(key, entry)

    def lookup(self, name: str) -> dict | None:
        e = self._by_alias.get(normalize(name))
        if not e:
            return None
        ra_d, dec_d, display = e
        return {"name": display, "ra_deg": ra_d, "dec_deg": dec_d, "ra_hours": ra_d / 15.0}


_catalog: Catalog | None = None


def get_catalog() -> Catalog:
    """Lazily-loaded process singleton."""
    global _catalog
    if _catalog is None:
        _catalog = Catalog()
        data = pathlib.Path(__file__).resolve().parents[1] / "data"
        for fname in ("openngc.tsv", "stars.tsv"):  # DSOs + named stars
            p = data / fname
            try:
                if p.exists():
                    _catalog.load(p)
            except Exception:
                log.warning("could not load catalog file %s", p, exc_info=True)
        log.info("local catalog loaded: %d names (OpenNGC + IAU star names)", _catalog.size)
    return _catalog
