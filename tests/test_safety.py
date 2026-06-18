"""Tests for the weather + safety state machine (offline, no hardware)."""
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cassa.agent.safety import SafetyMonitor  # noqa: E402


def _mon(**override):
    s = SimpleNamespace(safety_enabled=True, weather_device="", safety_stale_s=180.0,
                        safety_clear_delay_s=120.0, safety_humidity_warn=85.0,
                        safety_humidity_unsafe=95.0, safety_wind_unsafe=40.0, safety_cloud_unsafe=90.0)
    for k, v in override.items():
        setattr(s, k, v)
    state = SimpleNamespace(settings=s, dm=SimpleNamespace(client=None),
                            observatory=SimpleNamespace(location=SimpleNamespace(is_set=False)))
    return SafetyMonitor(SimpleNamespace(state=state))


def test_no_weather_is_unsafe():
    st, reasons = _mon()._evaluate()
    assert st == "unsafe" and "no weather data" in reasons


def test_good_weather_is_safe():
    m = _mon()
    m.set_weather({"humidity": 50, "wind_speed": 5})
    assert m._evaluate()[0] == "safe"


def test_rain_is_unsafe():
    m = _mon()
    m.set_weather({"rain": True, "humidity": 50})
    st, r = m._evaluate()
    assert st == "unsafe" and "rain" in r


def test_humidity_thresholds():
    m = _mon()
    m.set_weather({"humidity": 90})
    assert m._evaluate()[0] == "warn"
    m.set_weather({"humidity": 97})
    assert m._evaluate()[0] == "unsafe"


def test_wind_is_unsafe():
    m = _mon()
    m.set_weather({"humidity": 50, "wind_speed": 50})
    assert m._evaluate()[0] == "unsafe"


def test_stale_weather_is_unsafe():
    m = _mon()
    m.set_weather({"humidity": 50})
    m.weather["updated_at"] = time.time() - 300
    st, r = m._evaluate()
    assert st == "unsafe" and any("stale" in x for x in r)


def test_estop_is_fault():
    m = _mon()
    m.estop_trip()
    assert m._evaluate() == ("fault", ["emergency stop"])


def test_disabled_is_safe():
    assert _mon(safety_enabled=False)._evaluate()[0] == "safe"


def test_ok_to_dispatch_policy():
    m = _mon()
    m.state = "safe"
    assert m.ok_to_dispatch("auto") and m.ok_to_dispatch("attended")
    m.state = "warn"
    assert not m.ok_to_dispatch("auto") and m.ok_to_dispatch("attended")
    m.state = "unsafe"
    assert not m.ok_to_dispatch("auto") and not m.ok_to_dispatch("attended")
    m.override = True
    assert m.ok_to_dispatch("auto")


def test_hysteresis_holds_then_clears():
    m = _mon()
    m.state = "unsafe"
    m._ok_since = None
    assert m._apply_hysteresis("safe", [])[0] == "warn"     # just cleared → stabilizing
    m._ok_since = time.time() - 130                          # past the clear delay
    assert m._apply_hysteresis("safe", [])[0] == "safe"
