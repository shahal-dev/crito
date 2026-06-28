"""PHD2 guiding client.

Connects to PHD2's event server (TCP, default port 4400 — enable it in PHD2 via
Tools → Enable Server) and reads its newline-delimited JSON events. Tracks the live
guiding error (RA/Dec raw distances) for the dashboard's guiding plot, plus state
and RMS. Read-only by default; ``guide()`` / ``stop_guiding()`` issue PHD2 JSON-RPC
on the same socket.

PHD2 runs on the edge node with the guide camera + mount. Everything degrades
gracefully when PHD2 isn't reachable — the connector just retries quietly and the
UI shows "disconnected".
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("crito.agent.phd2")


class PHD2Client:
    def __init__(self, host: str, port: int = 4400):
        self.host = host
        self.port = int(port)
        self.connected = False
        self.state = "Stopped"
        self.samples: deque[dict] = deque(maxlen=400)  # {t, ra, dec, snr}
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._stop = False
        self._task: asyncio.Task | None = None
        self._id = 0

    # ----------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        await asyncio.sleep(2.0)
        while not self._stop:
            try:
                self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
                self.connected = True
                log.info("PHD2 connected %s:%s", self.host, self.port)
                while not self._stop:
                    line = await self._reader.readline()
                    if not line:
                        break
                    self._handle(line)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("PHD2 connection problem (%s:%s): %s", self.host, self.port, e)
            finally:
                self.connected = False
                self._writer = None
                self._reader = None
            if self._stop:
                break
            await asyncio.sleep(3.0)

    # --------------------------------------------------------------- events
    def _handle(self, line: bytes) -> None:
        try:
            evt = json.loads(line.decode().strip())
        except Exception:
            return
        e = evt.get("Event")
        if e == "GuideStep":
            self.state = "Guiding"
            self.samples.append({
                "t": datetime.now(timezone.utc).isoformat(),
                "ra": _num(evt.get("RADistanceRaw")),
                "dec": _num(evt.get("DECDistanceRaw")),
                "snr": _num(evt.get("SNR")),
            })
        elif e in ("GuidingStopped", "LoopingExposuresStopped"):
            self.state = "Stopped"
        elif e == "StartGuiding":
            self.state = "Guiding"
        elif e == "Paused":
            self.state = "Paused"
        elif e == "AppState":
            self.state = evt.get("State", self.state)

    # ----------------------------------------------------------- summaries
    def _rms(self, key: str) -> float | None:
        vals = [s[key] for s in self.samples if s.get(key) is not None]
        if not vals:
            return None
        mean = sum(vals) / len(vals)
        return round((sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5, 3)

    def summary(self) -> dict:
        return {
            "connected": self.connected,
            "state": self.state,
            "rms_ra": self._rms("ra"),
            "rms_dec": self._rms("dec"),
            "n": len(self.samples),
        }

    def graph(self, limit: int = 200) -> dict:
        return {**self.summary(), "samples": list(self.samples)[-limit:]}

    # -------------------------------------------------------------- control
    async def _rpc(self, method: str, params=None) -> None:
        if self._writer is None:
            raise RuntimeError("PHD2 not connected")
        self._id += 1
        msg = {"method": method, "id": self._id}
        if params is not None:
            msg["params"] = params
        self._writer.write((json.dumps(msg) + "\r\n").encode())
        await self._writer.drain()

    async def guide(self) -> None:
        """Start guiding (assumes PHD2 is connected to equipment + calibrated)."""
        await self._rpc("guide", [{"pixels": 2.0, "time": 8, "timeout": 40}, False])

    async def stop_guiding(self) -> None:
        await self._rpc("stop_capture")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
