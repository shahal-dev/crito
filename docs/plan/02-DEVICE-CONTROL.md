# 02 — Device Control Layer

This is the layer that actually talks to mounts, cameras, domes, focusers, filter
wheels, rotators, covers, weather stations, and seeing monitors. The goal: **one
uniform internal API** regardless of vendor, so the rest of CRITO never imports a
vendor SDK.

## 1. Standards we build on

| Standard | What it is | Use in CRITO |
|----------|-----------|--------------|
| **INDI** | Instrument-Neutral Distributed Interface; mature Linux ecosystem (used by KStars/Ekos). Huge native-Linux driver library for mounts/cameras/focusers. | **Primary** abstraction. Runs natively on the Linux edge node; covers all current IUB hardware (see §1a). |
| **ASCOM Alpaca** | Cross-platform REST/JSON API for astronomy devices (the modern, OS-independent successor to Windows COM ASCOM). Device types: Telescope, Camera, FilterWheel, Focuser, Rotator, Dome, CoverCalibrator, ObservingConditions, SafetyMonitor, Switch. | **Secondary** abstraction, added later for Windows/commercial or native-Alpaca **observatory-grade** gear (e.g., future 2 m-class setups). |
| **Vendor SDKs** | ZWO ASI, QHY, FLI, SBIG, PlaneWave PWI, Software Bisque, ToupTek, etc. | Only via an Alpaca/INDI driver wrapper — never called directly by CRITO business logic. |

**Decision:** CRITO defines its own thin **Device Abstraction Layer (DAL)** with one
interface per device *role*. Behind each role we plug an **adapter**: `IndiAdapter`,
`AlpacaAdapter`, or (rarely) a custom adapter. This means we support both ecosystems
and can mix them at the same site.

> **Adopted strategy: INDI-first, architect for both.** We implement the
> **`IndiAdapter` now** (it natively drives the entire current IUB rig on a Linux edge
> node with no Windows). The **`AlpacaAdapter` is a deferred stub** — built when the
> first Alpaca/Windows observatory device or a custom 2 m TCS arrives. The *value* is
> the role abstraction boundary, not running two live adapters from day one. The
> scheduler, pipeline, safety FSM, and UI sit above the DAL and never change when a
> new adapter is added. See [09-TECH-STACK.md](09-TECH-STACK.md) §1 for the rationale.

## 1a. Current IUB hardware → INDI driver map

Initial test rig, all driven natively by INDI on one Linux edge node — **no Windows,
no vendor appliance**:

| Device | Role | INDI driver | Connection notes |
|--------|------|-------------|------------------|
| **Sky-Watcher EQ6-R Pro** | mount | `indi_eqmod` (preferred) or `indi_synscan` | **EQDIR / USB-serial** cable directly to the mount's hand-controller port; bypass the SynScan handset for automation. Set baud + serial device in the driver. |
| **ToupTek Minicam8** | camera (imaging) | `indi_toupbase` | USB 3.0; set gain/offset/binning/ROI, cooler if present |
| **ToupTek AAF** | focuser | `indi_toupbase` (focuser interface) | USB; absolute + relative move, temp readout for temp-comp |
| **ToupTek GEM guide cam** | guide camera | `indi_toupbase` + **PHD2** | USB; PHD2 connects to it as an INDI camera; CRITO talks to PHD2 (see §4) |
| **StellaVita OTA** | optics | — | Optical tube; no driver. (Update this row if it carries any electronics.) |

`indi_toupbase` is a single driver family that exposes all three ToupTek devices, so
one driver package covers the cameras and the focuser.

### ⚠️ Use an *open* INDI host — not the closed StellaVita appliance

ToupTek's **StellaVita** mini-PC (an ASIAIR-style appliance) runs INDI *internally*
but typically **locks the INDI server behind its own app**, so it cannot be scripted,
cannot run the CRITO Site Agent, and cannot be orchestrated from the IUB core. **Do
not build CRITO on the StellaVita app.**

- **Edge node = an open Linux box you fully control**: a **Raspberry Pi 5 (8 GB)** or a
  small **x86 mini-PC** running **Ubuntu Server + `indi-full`**, or **StellarMate OS**
  (open, unlocked INDI on a Pi). Plug the EQ6-R (via EQDIR) and the three ToupTek USB
  devices straight into it; run `indiserver` with the drivers above + PHD2. CRITO's
  `IndiAdapter` connects to `localhost:7624`.
- **The ToupTek devices do NOT need StellaVita** — `indi_toupbase` runs on any Linux
  box, so the cameras/focuser plug straight into your own edge node.
- **Quick openness test for StellaVita** (might save buying a separate Pi): from a
  laptop running **KStars/Ekos**, add a remote INDI connection to
  `STELLAVITA_IP:7624`. If Ekos connects and the devices appear, StellaVita exposes an
  **open** INDI server and *may* serve as the edge host (if it also lets you install
  the Site Agent). If only the StellaVita app works, treat it as a closed appliance
  and use your own Linux box. Either way CRITO's code is identical — it only ever sees
  a standard INDI server on port **7624**.

> Note: the **autonomous vs operator-approve** modes you need live in the
> scheduler/execution layer ([04-PLANNING-SCHEDULING.md](04-PLANNING-SCHEDULING.md)
> §3 & §5), *above* the DAL — so they work the same regardless of INDI/Alpaca.

```
        CRITO internal API (roles)
   Mount · Camera · Dome · Focuser · FilterWheel · Rotator ·
   Cover · FlatPanel · WeatherSensor · SeeingMonitor · SafetyMonitor · Switch
                         │
                ┌────────┴────────┐
                ▼                 ▼
        IndiAdapter          AlpacaAdapter      (CustomAdapter)
        (INDI/TCP) ★now      (REST/JSON) later   (vendor / 2 m TCS)
                │                 │
   indiserver + drivers      Alpaca devices
   (indi_eqmod, indi_toupbase, PHD2 …)
```

## 2. Device roles & the operations CRITO needs

### Mount / Telescope
- `slew_to_radec`, `slew_to_altaz`, `sync`, `abort_slew`
- `track(on/off, rate)` (sidereal/lunar/solar/custom/king)
- `park`, `unpark`, `find_home`, `set_park`
- `pulse_guide(dir, ms)` and/or guide via PHD2 (below)
- `pier_side`, `meridian_flip` handling, slew-limit & horizon enforcement
- read: RA/Dec/Alt/Az, LST, slewing, tracking, at-park, at-home
- safety: software slew limits, horizon mask, never slew into the Sun (Sun-avoidance
  radius enforced at edge).

### Camera (imaging & guide)
- `start_exposure(seconds, light/dark/bias/flat)`, `abort_exposure`
- read: `image_ready`, download array, sensor temp, cooler power
- cooler control: `set_ccd_temperature`, `cooler_on/off`
- gain/offset/readout mode/binning/subframe (ROI)
- fast readout / streaming for focus & guiding
- metadata: pixel size, sensor dimensions, bayer pattern, egain

### Filter Wheel
- `set_position(index)`, read names/positions, focus-offset per filter

### Focuser
- `move_absolute`, `move_relative`, `halt`, read position & temp
- temperature-compensation curve support
- autofocus routine (V-curve / HFR minimization) — see §5

### Rotator
- `move_absolute` (sky PA), `sync`, mechanical ↔ sky angle mapping

### Dome / Roll-off roof
- `open_shutter`, `close_shutter`, `slew_to_azimuth`, `sync_to_mount` (slaving)
- `park`, `find_home`, read shutter state & azimuth
- **slaving**: dome azimuth tracks the mount automatically during exposures

### Cover / Flat panel (CoverCalibrator)
- `open_cover`, `close_cover`, `calibrator_on(brightness)`, `calibrator_off`
- used for flat-field acquisition and dust protection when idle

### ObservingConditions (weather sensors)
- read: temperature, humidity, dew point, pressure, wind speed/gust/direction,
  rain, sky temperature (cloud), sky brightness (SQM), seeing (if available)

### SafetyMonitor
- single boolean `is_safe` from a hardware/aggregated source; feeds the Safety FSM

### Switch
- power control (relays/PDU) for instrument power cycling, dew heaters, etc.

## 3. Internal device API shape

Every role is an async interface with: **connect / disconnect / status / capabilities
/ commands**, and emits **events** (`state_changed`, `progress`, `fault`). Example
(illustrative, language-agnostic):

```python
class Mount(Device):
    capabilities: MountCaps        # can_park, can_pulse_guide, can_set_tracking, ...
    async def slew_to_radec(self, ra_hours, dec_deg, *, blocking=False) -> CommandHandle
    async def abort_slew(self) -> None
    async def set_tracking(self, on: bool, rate: TrackingRate = SIDEREAL) -> None
    async def park(self) -> CommandHandle
    def status(self) -> MountStatus  # ra, dec, alt, az, slewing, tracking, at_park, pier_side
    # emits: state_changed, slew_progress, fault
```

`CommandHandle` is awaitable and also reports progress events so the UI can show a
live slew. Long operations are **non-blocking by default** with progress, never a
synchronous wait that blocks the agent.

## 4. Guiding

- Integrate **PHD2** (open-source autoguider) over its event/JSON-RPC socket at each
  site, OR implement an internal guider for simple setups.
- Operator can: start/stop guiding, see guide graph (RA/Dec error in arcsec), set
  guide star, dither between exposures.
- Guiding state is part of live telemetry; loss-of-guiding can pause a sequence.

## 5. Autofocus

- Routine at the edge: step focuser across a range, measure **HFR/FWHM** of stars
  per step, fit a V-curve, move to the minimum.
- Triggers: start of night, filter change (apply focus offset first), temperature
  delta threshold, time interval, or manual.
- Results logged (focus position vs temperature) to build/refine the temp-comp model.

## 6. Plate-solve-assisted pointing ("center on target")

A core convenience used in both manual and automated modes:
1. Slew to target RA/Dec (blind).
2. Take a short exposure, **plate-solve** locally (astrometry.net / ASTAP).
3. Compute pointing error from solved center vs target.
4. `sync` or offset-slew to correct; repeat until error < threshold (e.g., < 5″).
5. Optionally rotate field to requested PA using the rotator.

This makes pointing accurate regardless of mount model quality. See
[03-DATA-PIPELINE.md](03-DATA-PIPELINE.md) for the solver details.

## 7. Manual control surface (operator)

Every role gets a **manual control panel** in the UI (see [08-FRONTEND.md](08-FRONTEND.md)):
- Mount: N/S/E/W jog buttons + rate selector, RA/Dec goto box, sky map click-to-slew,
  park/unpark, tracking toggle, abort (big red).
- Camera: exposure box, loop/preview mode, cooler setpoint, gain/bin, live histogram.
- Dome: open/close/rotate, slave toggle.
- Focuser: in/out steps, goto, autofocus button.
- Filter wheel: filter dropdown.
- Power: per-outlet switches.

**Concurrency rule:** a device has one **controlling owner** at a time (operator
session or the executor). Taking manual control of a device **preempts** automation
for that device with an explicit, audited "take control" action, and automation is
notified/paused. This prevents two actors fighting over one mount.

## 8. Driver/config model

Each device entry in the inventory DB declares:

```yaml
- id: siteA-mount-01
  role: mount
  adapter: alpaca           # alpaca | indi | custom
  connection:
    base_url: http://10.8.0.11:11111
    device_number: 0
  capabilities_override: {}   # rarely needed; usually read from driver
  limits:
    horizon_profile_ref: siteA-horizon
    sun_avoidance_deg: 30
    slew_rate_max_deg_s: 4
```

Adding hardware = adding a config row + ensuring its Alpaca/INDI driver runs at the
site. **No CRITO code change.**

## 9. Testing & bring-up

- **Protocol unit tests** exercise the pure-Python INDI client (vector parsing,
  chunked streams, BLOB decode/inflate, command serialization) and the runtime
  discovery/binding logic with **no hardware and no running server** — these run in
  CI (`tests/test_protocol.py`, `tests/test_devices.py`).
- **Bring-up on real gear:** run `indiserver` with your device drivers on the
  observatory edge node, then **Scan → assign role → Connect** each device from the
  console. The full stack (planning → scheduling → execution → solve → archive)
  exercises the **same `IndiAdapter` + port-7624 path** regardless of brand, so
  adding hardware never touches CRITO code — see §8 above.

See **[06-WEATHER-SAFETY.md](06-WEATHER-SAFETY.md)** for how sensors drive safety.
