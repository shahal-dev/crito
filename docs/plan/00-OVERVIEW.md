# 00 — Overview & Goals

## 1. Vision

Build **CRITO**, a single platform from which IUB operators can control multiple
telescopes and domes located at different sites, with **full manual access to every
device** and optional automation for planning, scheduling, and autonomous execution.
The system supports the complete observing lifecycle:

**Plan → Schedule → Slew & acquire → Monitor live → Calibrate & plate-solve →
Archive → Retrieve remotely.**

It also continuously ingests **transient astronomical alerts** (supernovae, GRBs,
gravitational-wave counterparts, novae, etc.) and can turn them into follow-up
observations — either presented to an operator for one-click approval or executed
autonomously under policy.

## 2. Scope

### In scope
- Device control: mounts/telescopes, cameras (imaging + guiding), filter wheels,
  focusers, rotators, domes / roll-off roofs, covers, flat panels.
- Environmental sensing: weather stations, cloud/rain sensors, sky brightness,
  seeing monitors (DIMM/MASS), all-sky cameras.
- Manual operator control of all the above, in real time.
- Observation planning (target lists, visibility, constraints) and a scheduler.
- Plan execution engine with live status, pause/resume/abort.
- Image pipeline: calibration, **plate solving**, WCS injection, quality metrics.
- Storage: structured DB + FITS archive with provenance.
- Remote data access over **FTP/SFTP** (and HTTPS).
- Transient alert broker/scraper and follow-up triggering.
- Multi-site, multi-tenant (multiple instruments, multiple users/roles).

### Out of scope (initial phases)
- Building physical hardware / observatory civil works.
- Full survey-grade data-reduction pipelines (photometry catalogs, difference
  imaging) — CRITO produces calibrated, plate-solved frames; deep science reduction
  is a downstream consumer (hooks provided).
- Public data portal (can be added later on top of the archive API).

## 3. Primary actors

| Actor | Description | Typical actions |
|-------|-------------|-----------------|
| **Operator** | On-shift human at IUB driving the system | Manual control, approve plans, monitor, abort |
| **Observer / PI** | Astronomer requesting data | Submit targets & constraints, download data |
| **Scheduler (automation)** | Software | Pick next-best target, dispatch to sites |
| **Site Agent (automation)** | Edge software at each site | Execute device commands, enforce safety |
| **Alert Broker (automation)** | Software | Ingest transients, propose follow-up |
| **Admin** | System owner | Manage sites/instruments/users, configs |

## 4. Functional requirements (high level)

- **FR-1 Manual control:** Operators can directly command any device (slew, track,
  expose, focus, open/close dome, rotate) with sub-second feedback.
- **FR-2 Multi-site:** Operate ≥2 sites concurrently from one console; designed to
  scale to N sites.
- **FR-3 Planning:** Create target lists with constraints (altitude, moon, airmass,
  time windows, priority) and visualize observability.
- **FR-4 Scheduling:** Generate an ordered plan that respects constraints and
  optimizes a figure of merit; manual override always wins.
- **FR-5 Execution:** Run a plan autonomously with live progress; pause/resume/skip/
  abort at any granularity.
- **FR-6 Live telemetry:** Stream mount coordinates, camera state, dome state,
  weather, and per-exposure progress to operators in real time.
- **FR-7 Image handling:** Auto-calibrate (bias/dark/flat), **plate-solve**, write
  WCS + full provenance to FITS headers, compute QA metrics (FWHM, background, star
  count), generate previews.
- **FR-8 Storage:** Persist all frames + metadata; never mutate raw data.
- **FR-9 Remote retrieval:** Expose archived data via **SFTP/FTP** and HTTPS with
  per-user access control.
- **FR-10 Transient alerts:** Ingest alert streams, filter, and create candidate
  follow-up observations.
- **FR-11 Safety:** Automatic close/park on unsafe weather or fault, independent of
  network connectivity to IUB.
- **FR-12 Audit:** Every command and state transition is logged and attributable.

## 5. Non-functional requirements

| Area | Requirement |
|------|-------------|
| **Latency** | Manual command round-trip < 500 ms over healthy link; live telemetry ≤ 1 Hz minimum, configurable bursts |
| **Availability** | Site survives WAN outage and continues/cleans up safely; core targets 99.5% uptime |
| **Safety** | Fail-safe: on any doubt, stop tracking, park mount, close dome |
| **Scalability** | Add a site/instrument via config, not code changes |
| **Data integrity** | Checksums on every frame; raw immutable; backups |
| **Security** | All links encrypted (VPN + TLS); RBAC; full audit trail |
| **Portability** | Runs on Linux edge nodes; vendor-neutral device layer |
| **Observability** | Metrics, logs, traces for every service |

## 6. Key constraints & assumptions

- Sites have **intermittent / limited bandwidth**; design for disconnection and for
  transferring large FITS files asynchronously (FTP/SFTP, resumable).
- Hardware is heterogeneous (different mounts/cameras/domes per site) → strong
  abstraction layer required.
- Timekeeping: every node disciplined to **UTC via NTP/PTP**; all timestamps UTC.
- Operators may be remote (not physically at the site) → safety cannot depend on a
  human being present.
- Bangladesh sky conditions (monsoon, humidity) make **weather safety and dew/cloud
  handling first-class**, not an afterthought.

## 7. Glossary

| Term | Meaning |
|------|---------|
| **ASCOM Alpaca** | Cross-platform REST/JSON standard for astronomy device control |
| **INDI** | Instrument-Neutral Distributed Interface; Linux device control protocol |
| **WCS** | World Coordinate System — maps pixels ↔ sky coordinates (RA/Dec) |
| **Plate solving** | Determining the WCS of an image by matching star patterns |
| **FITS** | Flexible Image Transport System — standard astronomical image/file format |
| **Site Agent** | Edge service at a site that drives devices and enforces safety |
| **GCN** | General Coordinates Network — distributes transient/multimessenger alerts |
| **TNS** | Transient Name Server — official IAU registry of transients |
| **DIMM** | Differential Image Motion Monitor — measures atmospheric seeing |
| **Figure of merit** | Score the scheduler maximizes when choosing the next target |

See **[01-ARCHITECTURE.md](01-ARCHITECTURE.md)** for how these pieces fit together.
