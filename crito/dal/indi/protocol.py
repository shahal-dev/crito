"""Minimal async INDI protocol client (pure Python, no native dependencies).

Speaks the INDI XML wire protocol over TCP (default port 7624), so CRITO can drive
any ``indiserver`` running real device drivers — without pyindi-client / libindi
build deps.

The client maintains a cache of every device's properties (updated on each
def*/set*Vector message) and exposes simple accessors plus command helpers. BLOBs
(e.g. CCD images) are delivered to registered handlers, decoded and decompressed.

INDI protocol reference: messages are XML elements streamed over the socket with no
single enclosing root. We wrap the stream in a synthetic ``<indi>`` root and treat
each direct child as one complete message.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import xml.etree.ElementTree as ET
import zlib
from typing import Callable

log = logging.getLogger("crito.indi")

# handler(device, property_name, element_name, data_bytes, format) -> None
BlobHandler = Callable[[str, str, str, bytes, str], None]


class INDIClient:
    def __init__(self, host: str = "localhost", port: int = 7624):
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._parser: ET.XMLPullParser | None = None
        self._root: ET.Element | None = None
        self._depth = 0
        # state[device][property] = {"type":.., "state":.., "perm":.., "elements": {name: value}}
        self._state: dict[str, dict[str, dict]] = {}
        self._blob_handlers: list[BlobHandler] = []

    # ----------------------------------------------------------------- connection
    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        self._parser = ET.XMLPullParser(events=("start", "end"))
        self._parser.feed(b"<indi>")  # synthetic root to wrap the rootless stream
        self._root = None
        self._depth = 0
        self._state.clear()
        self._blob_handlers.clear()  # re-registered by adapters during setup
        self._read_task = asyncio.create_task(self._read_loop())
        await self.get_properties()
        log.info("connected to indiserver %s:%s", self.host, self.port)

    async def wait_closed(self) -> None:
        if self._read_task:
            await self._read_task

    async def close(self) -> None:
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    # -------------------------------------------------------------------- reading
    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                data = await self._reader.read(65536)
                if not data:
                    log.warning("indiserver closed the connection")
                    break
                self._feed(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("indi read loop error")

    def _feed(self, data: bytes) -> None:
        """Feed raw bytes to the XML parser and dispatch each completed message.

        The stream has no single root, so it is wrapped in a synthetic ``<indi>``
        element: each direct child (depth returns to 1) is one complete INDI message.
        """
        assert self._parser is not None
        self._parser.feed(data)
        for event, elem in self._parser.read_events():
            if event == "start":
                if self._root is None:
                    self._root = elem  # the <indi> wrapper
                self._depth += 1
            else:  # "end"
                self._depth -= 1
                if self._depth == 1:  # a complete top-level INDI message
                    self._handle(elem)
                    if self._root is not None:
                        self._root.clear()  # release children -> bounded memory

    def _handle(self, elem: ET.Element) -> None:
        tag = elem.tag
        if (tag.startswith("def") or tag.startswith("set")) and tag.endswith("Vector"):
            self._update_vector(elem, tag[3:-6])  # Number / Switch / Text / Light / BLOB
        elif tag == "delProperty":
            dev, name = elem.get("device"), elem.get("name")
            if dev in self._state:
                if name:
                    self._state[dev].pop(name, None)
                else:
                    self._state.pop(dev, None)
        elif tag == "message":
            msg = elem.get("message")
            if msg:
                log.debug("indi message: %s", msg)

    def _update_vector(self, elem: ET.Element, vtype: str) -> None:
        device, name = elem.get("device"), elem.get("name")
        if not device or not name:
            return
        prop = self._state.setdefault(device, {}).setdefault(name, {"elements": {}})
        prop["type"] = vtype
        if elem.get("state"):
            prop["state"] = elem.get("state")
        if elem.get("perm"):
            prop["perm"] = elem.get("perm")
        if vtype == "BLOB":
            self._dispatch_blobs(device, name, elem)
            return
        for child in elem:
            ename = child.get("name")
            if ename is None:
                continue
            text = (child.text or "").strip()
            if vtype == "Number":
                try:
                    prop["elements"][ename] = float(text)
                except ValueError:
                    prop["elements"][ename] = None
            elif vtype == "Switch":
                prop["elements"][ename] = text == "On"
            else:  # Text, Light
                prop["elements"][ename] = text

    def _dispatch_blobs(self, device: str, name: str, elem: ET.Element) -> None:
        for child in elem:
            payload = (child.text or "").strip()
            if not payload:
                continue
            try:
                raw = base64.b64decode(payload)
            except Exception:
                log.exception("bad base64 blob on %s.%s", device, name)
                continue
            fmt = child.get("format", "")
            if fmt.endswith(".z"):
                try:
                    raw = zlib.decompress(raw)
                    fmt = fmt[:-2]
                except Exception:
                    log.exception("blob decompress failed on %s.%s", device, name)
                    continue
            for handler in self._blob_handlers:
                try:
                    handler(device, name, child.get("name", ""), raw, fmt)
                except Exception:
                    log.exception("blob handler error")

    # -------------------------------------------------------------------- writing
    async def _send(self, xml: str) -> None:
        if not self._writer:
            raise RuntimeError("INDI client not connected")
        self._writer.write(xml.encode())
        await self._writer.drain()

    async def get_properties(self, device: str | None = None, name: str | None = None) -> None:
        attrs = ' version="1.7"'
        if device:
            attrs += f' device="{device}"'
        if name:
            attrs += f' name="{name}"'
        await self._send(f"<getProperties{attrs}/>")

    async def set_number(self, device: str, name: str, elements: dict[str, float]) -> None:
        parts = [f'<newNumberVector device="{device}" name="{name}">']
        parts += [f'<oneNumber name="{k}">{v}</oneNumber>' for k, v in elements.items()]
        parts.append("</newNumberVector>")
        await self._send("".join(parts))

    async def set_switch(self, device: str, name: str, elements: dict[str, bool]) -> None:
        parts = [f'<newSwitchVector device="{device}" name="{name}">']
        parts += [
            f'<oneSwitch name="{k}">{"On" if v else "Off"}</oneSwitch>'
            for k, v in elements.items()
        ]
        parts.append("</newSwitchVector>")
        await self._send("".join(parts))

    async def set_text(self, device: str, name: str, elements: dict[str, str]) -> None:
        parts = [f'<newTextVector device="{device}" name="{name}">']
        parts += [f'<oneText name="{k}">{v}</oneText>' for k, v in elements.items()]
        parts.append("</newTextVector>")
        await self._send("".join(parts))

    async def set_property(self, device: str, name: str, elements: dict) -> None:
        """Set a property without the caller knowing its type (Number/Switch/Text)."""
        try:
            ptype = self._state[device][name]["type"]
        except KeyError:
            ptype = "Text"
        if ptype == "Number":
            await self.set_number(device, name, {k: float(v) for k, v in elements.items()})
        elif ptype == "Switch":
            await self.set_switch(device, name, {k: bool(v) for k, v in elements.items()})
        else:
            await self.set_text(device, name, {k: str(v) for k, v in elements.items()})

    async def enable_blob(self, device: str, mode: str = "Also") -> None:
        # Never | Also | Only. BLOBs are not delivered to a client until enabled.
        await self._send(f'<enableBLOB device="{device}">{mode}</enableBLOB>')

    # --------------------------------------------------------------- state access
    def add_blob_handler(self, handler: BlobHandler) -> None:
        self._blob_handlers.append(handler)

    def element(self, device: str, prop: str, name: str, default=None):
        try:
            return self._state[device][prop]["elements"].get(name, default)
        except KeyError:
            return default

    def prop_state(self, device: str, prop: str, default=None):
        try:
            return self._state[device][prop].get("state", default)
        except KeyError:
            return default

    def has_prop(self, device: str, prop: str) -> bool:
        return device in self._state and prop in self._state[device]

    def device_names(self) -> list[str]:
        return sorted(self._state.keys())

    async def wait_for(self, predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
        """Poll the property cache until ``predicate`` is true or timeout elapses."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            if predicate():
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)
