# 06 — Weather, Seeing & Safety

Safety is the part that protects people and expensive hardware. It is **the highest-
priority subsystem** and is engineered to fail safe and to work **without any network
connection to IUB**.

## 1. Environmental sensing

| Sensor | Measures | Example hardware |
|--------|----------|------------------|
| **Weather station** | Temp, humidity, dew point, pressure, wind speed/gust/direction, rain | Davis Vantage, Lunatico AAG, generic Modbus/Alpaca ObservingConditions |
| **Cloud / sky sensor** | Sky-ambient IR temperature → cloud cover | Boltwood, AAG CloudWatcher |
| **Rain sensor** | Precipitation (binary + intensity) | Hydreon, AAG |
| **Sky Quality Meter (SQM)** | Sky brightness (mag/arcsec²) → moon/twilight/light pollution | Unihedron SQM |
| **All-sky camera** | Full-sky image → clouds, satellites, aurora, situational awareness | ASI all-sky, AllSkEye/INDI allsky |
| **Seeing monitor (DIMM/MASS)** | Atmospheric seeing (arcsec) | DIMM telescope, or derived from science-frame FWHM |
| **Wind/safety interlocks** | Hard limits | Dedicated relays / PLC |

All exposed to CRITO via the **ObservingConditions** and **SafetyMonitor** device
roles (Alpaca/INDI), plus the all-sky as an image source.

## 2. Seeing

- Primary: dedicated **DIMM** if available (best, instrument-independent).
- Fallback/secondary: derive seeing from **science-frame FWHM** in the QA pipeline
  (cheap, always available, but convolved with focus/tracking).
- Seeing feeds: the scheduler (assign demanding targets to good-seeing windows),
  autofocus cadence, and QA flagging.

## 3. Safety state machine (per site, runs at the edge)

```
        ┌────────┐  conditions good for T_clear   ┌────────┐
        │  SAFE   │ ◀──────────────────────────────│  WARN   │
        │ open OK │ ───────────────────────────────▶│ caution │
        └───┬────┘   any soft limit exceeded        └───┬────┘
            │                                            │ hard limit / persistence
            │ hard limit / sensor fault                  ▼
            │                                       ┌─────────┐
            └──────────────────────────────────────▶│ UNSAFE   │ ──▶ CLOSE dome,
                                                     │ closed   │     PARK mount,
                                                     └────┬────┘     ABORT exposure
                                                          │ sensor/device failure
                                                          ▼
                                                     ┌─────────┐
                                                     │  FAULT   │ ──▶ safest state +
                                                     │ human    │     page humans,
                                                     │ required │     no auto-reopen
                                                     └─────────┘
```

### Rules (all configurable per site)
- **Hard-close triggers (immediate UNSAFE):** rain detected, wind gust > limit,
  humidity > limit, cloud (sky-ambient ΔT) over threshold, sensor timeout/failure,
  loss of mains power (on UPS), dew point margin breached, manual emergency stop.
- **Soft/WARN triggers:** approaching any limit, high humidity trend, thin cloud,
  twilight ending. WARN may pause new slews but keep finishing the current exposure.
- **Hysteresis & persistence:** must be safe continuously for `T_clear` (e.g.,
  15–30 min) before auto-reopen — prevents flapping the dome in marginal conditions.
- **Fail-safe defaults:** *missing/old data = unsafe.* A dead sensor closes the dome.
  Every reading has a max age; stale → unsafe.
- **Sun-avoidance:** mount may never slew within N° of the Sun; enforced at the edge
  regardless of commands.

### Independence guarantees
- The Safety FSM runs **entirely on the Site Agent**. It needs no core connection.
- A **hardware watchdog / relay** (PLC or dedicated safety board) provides a last-
  resort close even if the Site Agent software itself crashes (e.g., relay drops →
  spring/gravity-assisted roof close, or "rain → close" wired in hardware).
- **UPS**: holds power long enough to park + close on outage; on restore the system
  boots into `SAFE`+parked and waits for explicit human clearance before observing.

## 4. Operator authority vs safety
- Operators can do anything **within** safe envelopes.
- Operators **cannot** override a hard-UNSAFE condition to open in the rain. They can
  acknowledge faults and, after a `FAULT`, must explicitly clear it to resume — with
  the reason audited.
- A clearly labeled **Emergency Stop** (per site + global) parks/closes everything
  instantly.

## 5. Central monitoring (core)
- The **Weather/Safety aggregator** shows every site's conditions and safety state on
  one dashboard: green/yellow/red per site, live sensor values, all-sky thumbnails,
  trend plots, and "time until safe-to-open".
- Drives the global situational-awareness banner in the operator console.
- Feeds the scheduler (don't dispatch to an unsafe/closing site) and notifications.

## 6. Notifications
On any safety transition (and faults), notify via Slack/Telegram/email/SMS:
`SITE A → UNSAFE (rain), dome closing, mount parking, exposure aborted @ 19:42 UTC`.
Escalate `FAULT` to on-call. All transitions logged to the telemetry DB for audit and
post-mortem.

## 7. Daytime & idle protection
- When idle: dome closed, mount parked, **covers closed** (dust/sun protection),
  cooler managed. Optional automatic daytime "all safe" verification.
- Never allow on-sky pointing while the Sun is up unless an explicit, supervised
  solar-observing mode (with proper filters) is configured — out of scope by default.

See **[07-DATABASE.md](07-DATABASE.md)** for how all this is persisted.
