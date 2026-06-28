"""Unit tests for the pure-Python INDI protocol client (stdlib only).

Runs under pytest (CI) or directly as a script (`python tests/test_protocol.py`)
so it can be checked without third-party dependencies installed.
"""
import asyncio
import base64
import os
import sys
import xml.etree.ElementTree as ET
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crito.dal.indi.protocol import INDIClient  # noqa: E402


def _client() -> INDIClient:
    c = INDIClient()
    c._parser = ET.XMLPullParser(events=("start", "end"))
    c._parser.feed(b"<indi>")
    c._root = None
    c._depth = 0
    return c


def test_vector_parsing_and_chunked_stream():
    c = _client()
    msg = (
        '<defSwitchVector device="EQMod Mount" name="CONNECTION" state="Ok" perm="rw">'
        '<defSwitch name="CONNECT">On</defSwitch><defSwitch name="DISCONNECT">Off</defSwitch>'
        '</defSwitchVector>'
        '<defNumberVector device="EQMod Mount" name="EQUATORIAL_EOD_COORD" state="Busy">'
        '<defNumber name="RA">5.59</defNumber><defNumber name="DEC">-5.39</defNumber>'
        '</defNumberVector>'
    )
    mid = len(msg) // 2
    c._feed(msg[:mid].encode())  # split mid-message to exercise buffering
    c._feed(msg[mid:].encode())

    assert c.element("EQMod Mount", "CONNECTION", "CONNECT") is True
    assert c.element("EQMod Mount", "CONNECTION", "DISCONNECT") is False
    assert c.element("EQMod Mount", "EQUATORIAL_EOD_COORD", "RA") == 5.59
    assert c.element("EQMod Mount", "EQUATORIAL_EOD_COORD", "DEC") == -5.39
    assert c.prop_state("EQMod Mount", "EQUATORIAL_EOD_COORD") == "Busy"
    assert c.has_prop("EQMod Mount", "CONNECTION")


def test_set_vector_updates_cache():
    c = _client()
    c._feed('<defNumberVector device="T" name="EQUATORIAL_EOD_COORD" state="Busy">'
            '<defNumber name="RA">5.59</defNumber></defNumberVector>'.encode())
    c._feed('<setNumberVector device="T" name="EQUATORIAL_EOD_COORD" state="Ok">'
            '<oneNumber name="RA">5.60</oneNumber></setNumberVector>'.encode())
    assert c.element("T", "EQUATORIAL_EOD_COORD", "RA") == 5.60
    assert c.prop_state("T", "EQUATORIAL_EOD_COORD") == "Ok"


def test_plain_blob_decoded():
    c = _client()
    blobs = []
    c.add_blob_handler(lambda d, n, e, data, fmt: blobs.append((d, data, fmt)))
    raw = b"SIMPLE  =  T / fake fits\n" * 8
    payload = base64.b64encode(raw).decode()
    c._feed(f'<setBLOBVector device="Toupcam" name="CCD1" state="Ok">'
            f'<oneBLOB name="CCD1" size="{len(raw)}" format=".fits">{payload}</oneBLOB>'
            f'</setBLOBVector>'.encode())
    assert len(blobs) == 1
    assert blobs[0] == ("Toupcam", raw, ".fits")


def test_compressed_blob_inflated():
    c = _client()
    blobs = []
    c.add_blob_handler(lambda d, n, e, data, fmt: blobs.append((data, fmt)))
    raw = b"x" * 500
    payload = base64.b64encode(zlib.compress(raw)).decode()
    c._feed(f'<setBLOBVector device="Toupcam" name="CCD1" state="Ok">'
            f'<oneBLOB name="CCD1" size="{len(raw)}" format=".fits.z">{payload}</oneBLOB>'
            f'</setBLOBVector>'.encode())
    assert blobs[0] == (raw, ".fits")  # inflated, suffix stripped


def test_del_property():
    c = _client()
    c._feed('<defSwitchVector device="T" name="CONNECTION" state="Ok">'
            '<defSwitch name="CONNECT">On</defSwitch></defSwitchVector>'.encode())
    assert c.has_prop("T", "CONNECTION")
    c._feed('<delProperty device="T" name="CONNECTION"/>'.encode())
    assert not c.has_prop("T", "CONNECTION")


def test_command_serialization():
    c = _client()
    sent = []

    async def fake_send(xml):
        sent.append(xml)

    c._send = fake_send
    asyncio.run(c.set_switch("T", "ON_COORD_SET", {"TRACK": True, "SLEW": False}))
    asyncio.run(c.set_number("T", "EQUATORIAL_EOD_COORD", {"RA": 5.59, "DEC": -5.39}))
    asyncio.run(c.enable_blob("Toupcam", "Also"))
    assert '<oneSwitch name="TRACK">On</oneSwitch>' in sent[0]
    assert '<oneSwitch name="SLEW">Off</oneSwitch>' in sent[0]
    assert '<oneNumber name="RA">5.59</oneNumber>' in sent[1]
    assert sent[2] == '<enableBLOB device="Toupcam">Also</enableBLOB>'


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("ALL PROTOCOL TESTS PASSED")
