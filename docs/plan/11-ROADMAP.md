# 11 — Roadmap (phased delivery)

Build in thin, working slices. Each phase ends with something **operable and
testable**, ideally against the **virtual (simulator) site** before real hardware.
This de-risks the project and lets IUB build software in parallel with procurement.

---

## Phase 0 — Foundations & virtual site (weeks 1–4)
**Goal:** scaffolding + a fully simulated telescope you can drive by hand.
- Repo, CI, Docker Compose, Postgres/Timescale/MinIO/Redis/NATS up.
- Device Abstraction Layer + **`IndiAdapter`**; wire to **INDI simulator drivers**
  (`indi_simulator_telescope`, `_ccd`, `_focus`, `_wheel`, `_dome`,
  `_weather`/`_gps`) — the virtual site mirrors the real INDI path exactly.
- Site Agent skeleton: Device Manager (INDI client → `localhost:7624`) + telemetry
  publisher.
- Minimal core API + a basic web console: **manual control of the simulated mount &
  camera**, live telemetry over WebSocket.
- ✅ *Milestone:* slew, expose, see a simulated image and live RA/Dec in the browser.

## Phase 1 — Manual control & imaging, real hardware (EQ6-R + ToupTek) (weeks 5–10)
**Goal:** drive the real test rig end-to-end, manually, via INDI.
- Harden `IndiAdapter`; cut over from simulator drivers to the real drivers
  (`indi_eqmod`, `indi_toupbase`) + PHD2 — see the **bring-up checklist** below.
- Full manual panels: mount jog/goto, camera (cooler/gain/bin/preview), focuser,
  guiding (PHD2), power switches. *(Dome/roll-off panel built but inactive until a
  dome is present.)*
- FITS authoring with full headers + checksums; save to edge disk.
- **SFTP edge→core ingest** + object-store archive + DB index + previews.
- Basic archive browser + **SFTP/FTP download gateway** (SFTPGo) for users.
- ✅ *Milestone:* operator manually images a target on the EQ6-R + Minicam8; file
  lands in the archive and is downloadable over SFTP/FTP.
- **Status:** implemented against the **virtual site** — capture → provenance FITS
  (headers + `CHECKSUM`/SHA-256) → local object store + SQLite index → archive API +
  browser → HTTPS/SFTP (SFTPGo) download; focuser + filter-wheel manual control.
  Remaining for real hardware: PHD2 guiding panel, camera cooler/gain/ROI controls,
  and the on-site EQDIR/`indi_toupbase` bring-up (see checklist above).

## Phase 2 — Plate solving, calibration & QA (weeks 9–14)
**Goal:** scientifically useful frames.
- Edge **ASTAP/astrometry.net** solve for **plate-solve-assisted pointing**
  ("center on target").
- Core calibration (bias/dark/flat) + master-frame builder.
- WCS injection, QA metrics (FWHM, stars, background, ellipticity), QA badges in UI.
- Autofocus routine (HFR V-curve) + temp-comp.
- ✅ *Milestone:* one-click "center on target" + auto-calibrated, plate-solved,
  QA-scored images.

## Phase 3 — Planning & scheduling & execution (weeks 13–20)
**Goal:** automate the night while keeping manual override.
- Astropy/Astroplan **observability engine** + planning UI (timelines, alt curves,
  observability grid).
- Programs/targets/requests/constraints data model.
- **Greedy scheduler** (next-best target) + plan/block/step execution engine.
- Execution monitor UI: live plan tree, pause/resume/skip/abort, reorder/insert.
- Calibration planning (twilight flats, dark sets).
- ✅ *Milestone:* dispatch and autonomously execute a night plan on one instrument,
  with full live monitoring and manual takeover.

## Phase 4 — Weather & safety hardening (weeks 19–24)
**Goal:** safe unattended operation.
- ObservingConditions + SafetyMonitor integration; all-sky camera feed.
- **Edge Safety FSM** (`SAFE/WARN/UNSAFE/FAULT`) with hysteresis, stale-data-=-unsafe,
  Sun-avoidance, hardware watchdog/relay, UPS-triggered park+close.
- Central weather/safety dashboard + notifications + Emergency Stop.
- Fault-injection test suite (rain/wind/sensor-timeout/link-drop).
- ✅ *Milestone:* simulated and real bad-weather events automatically close the dome &
  park the mount — even with the core link cut.

## Phase 5 — Multi-site (weeks 23–30)
**Goal:** operate ≥2 sites from one console.
- Second Site Agent; WireGuard mesh; per-site config-driven onboarding.
- Site-aware scheduler (assign targets by best site/airmass/weather).
- Fleet overview UI; multi-site live monitoring; per-site safety independence.
- Offline/disconnect handling: cached-plan autonomy + buffered telemetry/data sync.
- ✅ *Milestone:* two sites running concurrently, each surviving a WAN cut and
  re-syncing on reconnect.

## Phase 6 — Transient alert broker (weeks 29–36)
**Goal:** ingest transients and trigger follow-up.
- Ingest adapters: **GCN (Kafka)**, **ALeRCE/ANTARES/Lasair/Fink**, **TNS**, VOEvent;
  scrapers for ATel/MPC. Normalizer → canonical alert schema.
- Filter/scoring engine (UI-editable filters) + de-dup/cross-match + observability.
- ToO request creation: auto-execute (policy) or operator-approve; GW/neutrino
  localization-map **tiling** plans.
- Alerts inbox UI + notifications.
- ✅ *Milestone:* a real GW/GRB alert produces an observable, approvable follow-up plan
  end-to-end.

## Phase 7 — Production hardening & scale (weeks 35+)
**Goal:** robust, observable, maintainable operations.
- Full observability stack (Prometheus/Grafana/Loki/OTel/Sentry).
- Backups + DR drills; secrets in Vault; RBAC/MFA/SSO.
- Performance: telemetry fan-out at scale, archive cone-search tuning.
- Runbooks, on-call, staged edge updates.
- Optional: outbound reporting (TNS/GCN), downstream science-pipeline hooks,
  public/PI data portal.
- ✅ *Milestone:* fleet runs unattended-overnight reliably with alerting & recovery.

---

## Hardware bring-up checklist (Phase 0 → Phase 1 cutover)

The test rig: **Sky-Watcher EQ6-R Pro** + **ToupTek Minicam8** (imaging) + **ToupTek
AAF** (focuser) + **ToupTek GEM guide cam**, all on **one open Linux edge node** via
**INDI**. (See [02-DEVICE-CONTROL.md](02-DEVICE-CONTROL.md) §1a.)

### A. Edge node & OS
- [ ] Provision the edge box: **Raspberry Pi 5 (8 GB)** or x86 mini-PC, **Ubuntu
      Server LTS** (or StellarMate OS). *Do **not** use the closed StellaVita app.*
- [ ] (Optional) Run the StellaVita openness test: Ekos → `STELLAVITA_IP:7624`. If
      open and installable, it may serve as the edge host; otherwise use your own box.
- [ ] Install INDI: `sudo apt install indi-full` (drivers) + `indi-bin`; install
      **PHD2** (`phd2`).
- [ ] Set timezone to UTC; enable **chrony** (NTP) and confirm clock sync.
- [ ] Confirm USB power budget (powered USB 3.0 hub for the cameras if on a Pi).

### B. Mount — EQ6-R Pro
- [ ] Wire an **EQDIR / USB-serial** adapter from the PC to the mount's hand-controller
      port (bypass the SynScan handset for automation).
- [ ] Identify the serial device (`/dev/ttyUSB0` or `/dev/serial/by-id/...`); use the
      stable `by-id` path in config so it survives reboots.
- [ ] Start `indi_eqmod`; set port + baud; **connect**. Verify RA/Dec readout, N/S/E/W
      jog, tracking on/off, **park/unpark**, and `abort_slew`.
- [ ] Set the **site location** (lat/lon/elev) and confirm a goto lands sensibly.
- [ ] Configure **slew limits + horizon mask + Sun-avoidance** before any daytime test.
- [ ] Do a one-time **mount alignment/sync** (CASSA's plate-solve sync refines this in
      Phase 2).

### C. Cameras & focuser — ToupTek (`indi_toupbase`)
- [ ] Plug **Minicam8**, **AAF**, **GEM guide cam** into USB 3.0; start `indi_toupbase`.
- [ ] Verify each enumerates; note exact device names (for config rows).
- [ ] **Minicam8:** take a bias, dark, and a short light; confirm gain/offset/binning/
      ROI controls and (if present) cooler setpoint behave. Record pixel size + sensor
      dims for FITS headers + plate-solve scale.
- [ ] **AAF focuser:** confirm absolute + relative moves, max position, and temperature
      readout (needed for temp-comp later).
- [ ] **Guide cam:** confirm frames; reserve it for PHD2 (step D).

### D. Guiding — PHD2
- [ ] Launch PHD2; connect mount via **INDI** and the **GEM guide cam** via INDI.
- [ ] Run **calibration** near the celestial equator; confirm guide pulses move the star
      the right way. Tune for the EQ6-R.
- [ ] Enable PHD2's **server/event API**; confirm CASSA can read the guide graph and
      issue start/stop/dither.

### E. CASSA cutover (simulator → real)
- [ ] Swap the virtual-site config from `indi_simulator_*` to the **real drivers**
      (`indi_eqmod`, `indi_toupbase`) — *config only, no code change*.
- [ ] Confirm CASSA's `IndiAdapter` connects to `localhost:7624` and all roles map:
      mount, camera (imaging), focuser, guide (via PHD2).
- [ ] End-to-end manual test: **slew → focus → expose → readout → FITS with full
      headers + checksum → SFTP to core → archived → preview in UI → download over
      SFTP/FTP.**
- [ ] Install **astrometry.net index files** matched to the Minicam8 + OTA field of
      view (and/or **ASTAP** star DB) for Phase 2 plate solving.
- [ ] Capture a known-WCS test frame as a plate-solve regression fixture.

### F. Safety pre-checks (even pre-dome)
- [ ] Define park-on-fault behavior; confirm the mount parks on driver disconnect.
- [ ] Wire a basic weather/cloud sensor as `ObservingConditions` when available; until
      then, operate attended only (no autonomous mode) — see
      [06-WEATHER-SAFETY.md](06-WEATHER-SAFETY.md).

## Sequencing notes
- **Phases overlap** (dates are indicative, assume a small team). The dependency
  spine is: 0 → 1 → 2 → 3 → 4, with 5/6 layering on top.
- **Safety (Phase 4)** must land before any *unattended* operation, even though
  device control comes earlier.
- Keep the **virtual site** working throughout — it's your regression net and lets
  development continue regardless of hardware/weather availability.
- Procurement of mounts/cameras/domes/sensors and **astrometry.net index files**
  should start during Phase 0–1 so hardware is ready for Phase 1–2.

## Team & skills (rough)
- 1–2 backend/Python (devices, services, pipeline)
- 1 frontend (React/TS)
- 1 devops/infra (VPN, deploy, observability) — can be shared
- Domain input from an astronomer (constraints, calibration, alert filters)
- Site technician(s) for hardware install & safety wiring

## Top risks & mitigations
| Risk | Mitigation |
|------|------------|
| Hardware/driver incompatibility | INDI-first via the DAL (Alpaca added later); INDI simulator-first; buy gear with known INDI drivers |
| Remote-site network flakiness | Edge autonomy, durable bus, resumable transfers, dial-out VPN |
| Weather damage (monsoon/dew) | Fail-safe Safety FSM + hardware relay + UPS; conservative limits |
| Scope creep | Phase gates; CASSA stops at calibrated+solved frames, science pipelines are hooks |
| Plate-solve reliability | Hint-based + blind fallback; correct index files per FoV; QA on residuals |
| Losing alerts | Durable Kafka/JetStream consumers; archive every raw packet |
```
