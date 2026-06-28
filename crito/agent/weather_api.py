"""Weather API auto-feed for the safety monitor.

Polls a weather API for the site's coordinates and pushes normalized readings into
the SafetyMonitor (same path as ``POST /api/safety/weather``). Default provider is
Open-Meteo (free, no key); OpenWeatherMap is supported with a key.

NOTE: a regional weather API is a COARSE input — it reports the nearest station /
grid cell and updates slowly, so it won't catch a cloud bank or a shower passing
over the dome. It's far better than nothing, but real safety wants an on-site rain
detector + cloud (sky-temperature) sensor feeding an INDI weather device.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger("crito.agent.weather_api")

_OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
_OWM = "https://api.openweathermap.org/data/2.5/weather"


def _condition(rain: bool, clouds) -> str:
    if rain:
        return "Rain"
    if clouds is None:
        return "—"
    if clouds >= 80:
        return "Cloudy"
    if clouds >= 40:
        return "Partly cloudy"
    return "Clear"


class WeatherApiPoller:
    def __init__(self, app):
        self.app = app
        self.s = app.state.settings
        self._stop = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        await asyncio.sleep(4.0)
        loc = self.app.state.observatory.location
        if not self.s.weather_api or not getattr(loc, "is_set", False):
            log.info("weather API feed disabled (provider=%r, location set=%s)",
                     self.s.weather_api, getattr(loc, "is_set", False))
            return
        log.info("weather API feed: %s for (%.3f, %.3f) every %ss",
                 self.s.weather_api, loc.latitude_deg, loc.longitude_deg, self.s.weather_poll_s)
        while not self._stop:
            try:
                w = await self._fetch(loc.latitude_deg, loc.longitude_deg)
                if w:
                    self.app.state.safety.set_weather(w)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("weather API poll failed: %s: %s", type(e).__name__, e)
            await asyncio.sleep(max(120, self.s.weather_poll_s))

    async def _fetch(self, lat: float, lon: float) -> dict | None:
        if self.s.weather_api == "openweather":
            return await self._openweather(lat, lon)
        return await self._open_meteo(lat, lon)

    async def _open_meteo(self, lat: float, lon: float) -> dict | None:
        params = {
            "latitude": lat, "longitude": lon, "wind_speed_unit": "kmh", "timezone": "auto",
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,cloud_cover,precipitation,rain",
        }
        r = await self.app.state.http.get(_OPEN_METEO, params=params, timeout=15.0)
        r.raise_for_status()
        c = r.json().get("current", {})
        clouds = c.get("cloud_cover")
        rain = (c.get("rain") or 0) > 0 or (c.get("precipitation") or 0) > 0
        return {"humidity": c.get("relative_humidity_2m"), "wind_speed": c.get("wind_speed_10m"),
                "temperature": c.get("temperature_2m"), "clouds": clouds, "rain": rain,
                "condition": _condition(rain, clouds), "source": "open-meteo"}

    async def _openweather(self, lat: float, lon: float) -> dict | None:
        if not self.s.weather_api_key:
            log.warning("weather_api=openweather but no CRITO_WEATHER_API_KEY set")
            return None
        params = {"lat": lat, "lon": lon, "appid": self.s.weather_api_key, "units": "metric"}
        r = await self.app.state.http.get(_OWM, params=params, timeout=15.0)
        r.raise_for_status()
        d = r.json()
        main, wind = d.get("main", {}), d.get("wind", {})
        clouds = (d.get("clouds") or {}).get("all")
        cond = (d.get("weather") or [{}])[0].get("main", "")
        rain = "rain" in d or cond.lower() == "rain"
        return {"humidity": main.get("humidity"),
                "wind_speed": round((wind.get("speed") or 0) * 3.6, 1),  # m/s → km/h
                "temperature": main.get("temp"), "clouds": clouds, "rain": rain,
                "condition": cond or _condition(rain, clouds), "source": "openweather"}
