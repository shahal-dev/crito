"""Tests for runtime device discovery + binding persistence (no hardware)."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crito.agent.device_manager import DeviceManager, _roles_for  # noqa: E402


def _settings(tmp_path):
    return SimpleNamespace(
        indi_host="localhost", indi_port=7624,
        bindings_path=str(tmp_path / "bindings.json"),
        site_id="virtual", instrument_id="vinstr", observer="CRITO",
        telescope_name="T", instrument_name="C",
    )


def test_roles_from_interface_bitmask():
    assert _roles_for(1) == ["mount"]
    assert _roles_for(2) == ["camera"]
    assert _roles_for(8) == ["focuser"]
    assert _roles_for(16) == ["filter"]
    assert _roles_for(2 | 8) == ["camera", "focuser"]
    assert _roles_for(1 | 4) == ["mount", "guide"]  # telescope + guider
    assert _roles_for(2 | 16) == ["camera", "filter"]  # QHY camera+filter bundle


def test_list_devices_maps_state(tmp_path):
    dm = DeviceManager(_settings(tmp_path))
    dm.client._state = {
        "EQMod Mount": {
            "DRIVER_INFO": {"type": "Text", "elements": {"DRIVER_INTERFACE": "1"}},
            "CONNECTION": {"type": "Switch", "elements": {"CONNECT": False}},
            "DEVICE_PORT": {"type": "Text", "elements": {"PORT": "/dev/ttyUSB0"}},
        },
        "CCD Sim": {
            "DRIVER_INFO": {"type": "Text", "elements": {"DRIVER_INTERFACE": "2"}},
            "CONNECTION": {"type": "Switch", "elements": {"CONNECT": True}},
        },
    }
    devs = {d["device"]: d for d in dm.list_devices()}

    assert devs["EQMod Mount"]["roles"] == ["mount"]
    assert devs["EQMod Mount"]["has_port"] is True
    assert devs["EQMod Mount"]["port"] == "/dev/ttyUSB0"
    assert devs["EQMod Mount"]["connected"] is False
    assert devs["CCD Sim"]["roles"] == ["camera"]
    assert devs["CCD Sim"]["connected"] is True


def test_binding_persistence_round_trip(tmp_path):
    s = _settings(tmp_path)
    dm = DeviceManager(s)
    dm._server = {"host": "10.0.0.5", "port": 7624}
    dm._bindings = {"mount": {"device": "EQMod Mount", "params": {"DEVICE_PORT": {"PORT": "/dev/ttyUSB0"}}}}
    dm._save_bindings()

    dm2 = DeviceManager(s)  # re-reads the file on construction
    assert dm2._server["host"] == "10.0.0.5"
    assert dm2._bindings["mount"]["device"] == "EQMod Mount"
    assert dm2._bindings["mount"]["params"]["DEVICE_PORT"]["PORT"] == "/dev/ttyUSB0"
