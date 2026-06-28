"""Camera characterization driver — takes the calibration frames over INDI and
writes the table that :mod:`crito.transient.exposure` consumes.

It reuses CRITO's own INDI client + camera adapter (``crito.dal``), so it talks to
the same ``indiserver`` the rest of the system uses. The heavy lifting (gain, read
noise, dark current) is delegated to :mod:`crito.calib.analysis`; this file is only
the orchestration: set gain/offset/temperature, capture the right frame pairs, and
serialize the result.

Because read noise and full well change with the gain setting on a CMOS sensor, it
sweeps a list of gains. Read noise + system gain at each gain need a uniform light
source (flat panel or twilight sky) at a steady ~30–60 % full-well level; dark
current needs a covered/dark sensor at the operating temperature.

Run it (with an ``indiserver`` up and the camera connected)::

    python -m crito.calib.characterize --device "QHY CCD QHYminiCam8" \\
        --gains 0,60,120,200 --offset 30 --temp -10 \\
        --flat-exptime 2.0 --dark-exptimes 5,30,120 --out calibration/minicam8.yaml

Omit ``--flat-exptime`` (no light source) to record read noise in ADU only — you
then need the datasheet system gain to convert it to electrons.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from ..dal.indi.protocol import INDIClient
from ..dal.indi_adapter import IndiCamera
from . import analysis

log = logging.getLogger("crito.calib")

# Most CMOS drivers (QHY included) expose Gain/Offset under CCD_CONTROLS; some use
# the newer standalone CCD_GAIN / CCD_OFFSET vectors. We try CCD_CONTROLS first.
_FRAME = {"light": "FRAME_LIGHT", "dark": "FRAME_DARK",
          "bias": "FRAME_BIAS", "flat": "FRAME_FLAT"}
_TEMP_PROP, _TEMP_ELEM = "CCD_TEMPERATURE", "CCD_TEMPERATURE_VALUE"


# --------------------------------------------------------------- INDI helpers
async def set_control(client: INDIClient, device: str, **controls: float) -> None:
    """Set Gain/Offset (or any CCD control), tolerating either property layout."""
    if client.has_prop(device, "CCD_CONTROLS"):
        await client.set_number(device, "CCD_CONTROLS", controls)
        return
    for key, val in controls.items():
        prop = f"CCD_{key.upper()}"
        if client.has_prop(device, prop):
            await client.set_number(device, prop, {key.upper(): float(val)})
        else:
            log.warning("no INDI property for control %s on %s", key, device)


async def set_frame_type(client: INDIClient, device: str, kind: str) -> None:
    if not client.has_prop(device, "CCD_FRAME_TYPE"):
        return  # shutterless sensor: a 'dark' is just an exposure with the cap on
    target = _FRAME[kind]
    await client.set_switch(device, "CCD_FRAME_TYPE",
                            {v: (v == target) for v in _FRAME.values()})


async def set_temperature(client: INDIClient, device: str, temp_c: float,
                          tol: float = 0.5, timeout: float = 900.0) -> bool:
    if not client.has_prop(device, _TEMP_PROP):
        log.warning("%s has no %s — skipping cooling", device, _TEMP_PROP)
        return False
    await client.set_number(device, _TEMP_PROP, {_TEMP_ELEM: temp_c})
    log.info("cooling to %.1f °C (tol ±%.1f) …", temp_c, tol)
    return await client.wait_for(
        lambda: abs((client.element(device, _TEMP_PROP, _TEMP_ELEM) or 1e9) - temp_c) <= tol,
        timeout)


async def _capture_array(cam: IndiCamera, seconds: float):
    return analysis.load_fits_array(await cam.capture(seconds))


# --------------------------------------------------------------- measurements
async def measure_gain(cam, client, device, gain, offset, *, flat_exptime, bias_exptime,
                       frac) -> dict:
    """Read noise (+ system gain, if a flat is taken) at one gain setting."""
    await set_control(client, device, Gain=gain, Offset=offset)
    await asyncio.sleep(0.5)

    await set_frame_type(client, device, "bias")
    b1 = await _capture_array(cam, bias_exptime)
    b2 = await _capture_array(cam, bias_exptime)
    bias = analysis.frame_level(b1, frac)
    rn_adu = analysis.read_noise_adu(b1, b2, frac)
    entry = {"gain": gain, "bias_adu": round(bias, 1), "read_noise_adu": round(rn_adu, 3)}

    if flat_exptime and flat_exptime > 0:
        await set_frame_type(client, device, "flat")
        f1 = await _capture_array(cam, flat_exptime)
        f2 = await _capture_array(cam, flat_exptime)
        res = analysis.gain_read_noise(f1, f2, b1, b2, frac)
        entry.update(res.dict())
        log.info("gain %4s: flat %.0f ADU  gain %.3f e-/ADU  read noise %.2f e-",
                 gain, res.signal_adu, res.gain_e_per_adu, res.read_noise_e)
        if res.signal_adu < 0.1 * 65535 or res.signal_adu > 0.7 * 65535:
            log.warning("flat level %.0f ADU is outside ~10–70%% — adjust illumination/"
                        "exposure for a cleaner gain measurement", res.signal_adu)
    else:
        log.info("gain %4s: read noise %.2f ADU (no flat → electrons unknown)", gain, rn_adu)
    return entry


async def measure_dark(cam, client, device, gain, offset, gain_e_per_adu, *, exptimes,
                       bias_exptime, frac) -> float:
    """Dark current (e-/s/pixel) at the operating temperature for one gain."""
    await set_control(client, device, Gain=gain, Offset=offset)
    await asyncio.sleep(0.5)
    await set_frame_type(client, device, "bias")
    bias = analysis.frame_level(await _capture_array(cam, bias_exptime), frac)

    await set_frame_type(client, device, "dark")
    darks = [await _capture_array(cam, t) for t in exptimes]
    d = analysis.dark_current_series(darks, exptimes, bias, gain_e_per_adu, frac)
    log.info("dark current: %.4f e-/s/pixel", d)
    return d


# --------------------------------------------------------------- orchestration
async def run(args) -> dict:
    client = INDIClient(args.host, args.port)
    await client.connect()
    cam = IndiCamera(client, args.device)
    try:
        await cam.connect(timeout=args.connect_timeout)
        if args.temp is not None:
            if not await set_temperature(client, args.device, args.temp, timeout=args.cool_timeout):
                log.warning("temperature not reached within timeout — proceeding anyway")

        gains, offset, frac = args.gains, args.offset, args.frac
        per_gain = {}
        for g in gains:
            per_gain[str(g)] = await measure_gain(
                cam, client, args.device, g, offset,
                flat_exptime=args.flat_exptime, bias_exptime=args.bias_exptime, frac=frac)

        dark_table = {}
        if args.dark_exptimes:
            dg = args.dark_gain if args.dark_gain is not None else gains[0]
            g_e = per_gain.get(str(dg), {}).get("gain_e_per_adu")
            if g_e is None:
                log.warning("no measured gain at %s (no flats?) — dark current will be in "
                            "ADU/s; supply a datasheet gain to convert", dg)
                g_e = 1.0
            d = await measure_dark(cam, client, args.device, dg, offset, g_e,
                                   exptimes=args.dark_exptimes, bias_exptime=args.bias_exptime,
                                   frac=frac)
            if args.temp is not None:
                dark_table[str(args.temp)] = round(d, 5)

        table = {
            "camera": args.device,
            "sensor": args.sensor,
            "pixel_size_um": args.pixel_size,
            "offset": offset,
            "temperature_c": args.temp,
            "gains": {
                k: {"read_noise_e": v.get("read_noise_e"),
                    "full_well_e": None,  # fill from datasheet or a saturation flat
                    "system_gain_e_per_adu": v.get("gain_e_per_adu"),
                    "read_noise_adu": v.get("read_noise_adu"),
                    "bias_adu": v.get("bias_adu")}
                for k, v in per_gain.items()
            },
            "dark_current_e_per_s": dark_table,
            "filters": {},  # measure on-sky with analysis.sky_rate / zero_point
        }
        return table
    finally:
        await client.close()


def _parse_floats(s: str) -> list[float]:
    return [float(x) for x in s.split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m crito.calib.characterize",
        description="Measure per-gain read noise + system gain and the dark current "
                    "of a CMOS camera over INDI, and write a calibration YAML.")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=7624)
    p.add_argument("--device", required=True, help="INDI camera label (e.g. 'QHY CCD QHYminiCam8')")
    p.add_argument("--sensor", default=None, help="sensor name for the table (e.g. IMX585)")
    p.add_argument("--pixel-size", type=float, default=2.9, dest="pixel_size")
    p.add_argument("--gains", type=_parse_floats, required=True, help="comma list, e.g. 0,60,120,200")
    p.add_argument("--offset", type=float, default=30.0)
    p.add_argument("--temp", type=float, default=None, help="cool to this °C before measuring")
    p.add_argument("--flat-exptime", type=float, default=0.0,
                   help="flat exposure (s) with a uniform light source; 0 = read noise only")
    p.add_argument("--bias-exptime", type=float, default=0.0, help="bias exposure (s), ~min")
    p.add_argument("--dark-exptimes", type=_parse_floats, default=None,
                   help="comma list of dark exposures (s), e.g. 5,30,120")
    p.add_argument("--dark-gain", type=float, default=None, help="gain for darks (default: first)")
    p.add_argument("--frac", type=float, default=0.5, help="central box fraction analysed")
    p.add_argument("--connect-timeout", type=float, default=20.0)
    p.add_argument("--cool-timeout", type=float, default=900.0)
    p.add_argument("--out", default=None, help="write calibration YAML here (else stdout)")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    table = asyncio.run(run(a))

    import yaml
    text = yaml.safe_dump(table, sort_keys=False)
    if a.out:
        import pathlib
        pathlib.Path(a.out).write_text(text)
        log.info("wrote %s", a.out)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
