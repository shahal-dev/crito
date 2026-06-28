# 01 — System Architecture

## 1. Topology: edge + core

CRITO is a **two-tier distributed system**:

- **CRITO Core** — central services hosted at IUB (or a small cloud VM with a VPN
  back to IUB). Owns planning, scheduling, the archive, the alert broker, the API,
  and the operator console. Source of truth.
- **Site Agents** — one edge node per physical site. Drives that site's hardware,
  enforces local safety, buffers data, and stays operable when the WAN link drops.

```
┌───────────────────────────── CRITO CORE (IUB) ─────────────────────────────┐
│                                                                            │
│  ┌──────────┐   ┌────────────┐   ┌──────────────┐   ┌──────────────────┐   │
│  │  Web UI   │──▶│  API GW /  │──▶│  Scheduler    │   │  Alert Broker     │   │
│  │ (console) │   │  BFF       │   │  service      │   │  (ingest/filter)  │   │
│  └──────────┘   └─────┬──────┘   └──────┬───────┘   └────────┬─────────┘   │
│        ▲              │                 │                    │             │
│        │  WSS         ▼                 ▼                    ▼             │
│  ┌─────┴──────┐  ┌──────────────────── MESSAGE BUS (NATS/MQTT) ──────────┐ │
│  │ Realtime    │  └──────┬───────────────┬───────────────┬───────────────┘ │
│  │ gateway     │         │               │               │                 │
│  └─────────────┘   ┌─────┴─────┐   ┌──────┴──────┐  ┌─────┴──────┐          │
│                    │ Postgres  │   │ TimescaleDB │  │  FITS object│          │
│                    │ (+PostGIS)│   │ (telemetry) │  │  store (S3) │          │
│                    └───────────┘   └─────────────┘  └────────────┘          │
└────────────────────────────────┬───────────────────────────────────────────┘
                                  │  WireGuard VPN + TLS, message bus bridge
       ┌──────────────────────────┼──────────────────────────┐
       ▼                          ▼                           ▼
┌──────────────┐          ┌──────────────┐            ┌──────────────┐
│  SITE AGENT A │          │  SITE AGENT B │   ...      │  SITE AGENT N │
│ ──────────── │          │ ──────────── │            │ ──────────── │
│ Device mgr   │          │ Device mgr   │            │ Device mgr   │
│ Safety FSM   │          │ Safety FSM   │            │ Safety FSM   │
│ Seq executor │          │ Seq executor │            │ Seq executor │
│ Solve worker │          │ Solve worker │            │ Solve worker │
│ Local buffer │          │ Local buffer │            │ Local buffer │
│ Alpaca/INDI  │          │ Alpaca/INDI  │            │ Alpaca/INDI  │
└──────┬───────┘          └──────┬───────┘            └──────┬───────┘
   hardware                  hardware                    hardware
```

## 2. Core services (microservice-ish, can start as a modular monolith)

| Service | Responsibility |
|---------|----------------|
| **API Gateway / BFF** | Single authenticated entry point; REST + WebSocket; aggregates for the UI |
| **Identity & RBAC** | Users, roles, sessions, API tokens, audit |
| **Inventory service** | Sites, instruments, devices, capabilities, config |
| **Planning service** | Target lists, observability calculations, constraints |
| **Scheduler service** | Builds/optimizes observing plans; dispatches blocks |
| **Execution coordinator** | Tracks plan/block/exposure state across sites |
| **Realtime gateway** | Fan-out of telemetry & events to browsers (WebSocket/SSE) |
| **Alert broker** | Ingests transient streams, filters, proposes follow-up |
| **Image/archive service** | Ingests frames, metadata, previews, serves the archive |
| **Solve orchestrator** | Routes plate-solve jobs (edge-first, core fallback) |
| **Notification service** | Email/Slack/Telegram/webhook on events & faults |
| **Weather/safety aggregator** | Central view of all sites' conditions & safety state |

> **Start as a modular monolith** (one deployable, clear module boundaries) and
> split out the high-load parts (realtime gateway, image service, alert broker)
> into separate services as load grows. Don't pay microservice tax on day one.

## 3. Site Agent (edge node) modules

Runs on a rugged mini-PC / industrial NUC at each site (Linux). Self-contained:

| Module | Responsibility |
|--------|----------------|
| **Device Manager** | Holds connections to all local devices via Alpaca/INDI; exposes a uniform internal API |
| **Safety FSM** | Independent finite-state machine: `SAFE ⇄ WARN ⇄ UNSAFE ⇄ FAULT`; can force park/close with no network |
| **Sequence Executor** | Runs observing blocks dispatched from core; reports progress; works from a cached plan if disconnected |
| **Telemetry Publisher** | Streams device + environment state to the bus (and buffers when offline) |
| **Plate-solve Worker** | Local astrometry.net / ASTAP solve for fast pointing correction |
| **Data Spooler** | Writes FITS locally, computes checksums, queues for upload (resumable FTP/SFTP) |
| **Local Store** | SQLite/Parquet ring buffer for telemetry & a frame queue while offline |
| **Watchdog** | Hardware/software watchdog; restarts modules, escalates faults |

**Edge autonomy contract:** if the WAN link drops mid-exposure, the Site Agent (a)
finishes or safely aborts the current exposure, (b) continues a *cached* plan only
if policy allows, (c) **always** honors the Safety FSM, and (d) buffers all data and
telemetry for later sync. Manual commands require the link (no remote human = no new
manual moves), but safety actions never do.

## 4. Communication patterns

| Path | Transport | Why |
|------|-----------|-----|
| Browser ↔ Core | HTTPS (REST) + WSS (WebSocket) | Request/response + live push |
| Core ↔ Site Agent (control) | Message bus over VPN (**NATS** or **MQTT**) | Async, reconnect-tolerant, pub/sub + request/reply |
| Core ↔ Site Agent (bulk data) | **SFTP/FTP** or HTTPS multipart, resumable | Large FITS files, throttled, off the control path |
| Device ↔ Site Agent | **Alpaca REST** / **INDI TCP** | Standard, vendor-neutral |
| Service ↔ Service (core) | gRPC or REST internally; bus for events | Simplicity + decoupling |

**Why a message bus for control:** telescope sites have flaky links. A broker with
durable subscriptions (NATS JetStream / MQTT with QoS) means commands and telemetry
survive brief disconnects and reconnect cleanly, with at-least-once delivery and
clear ordering per device. Avoid raw long-lived HTTP for edge control.

### Command/response model
- Commands are **addressed messages**: `site.A.mount.slew` with a request id.
- Every command gets an **ack** (accepted/rejected) then **terminal result**
  (done/failed/aborted), plus interim progress events.
- Commands are **idempotent where possible** and carry a TTL so a stale command
  delivered after a reconnect is rejected, not executed.

## 5. State & data flow (one exposure, end to end)

```
Operator/Scheduler ── slew+expose request ──▶ Execution coordinator
        │                                          │ dispatch block
        │                                          ▼
        │                                   Site Agent (Seq Executor)
        │                                     ├─ Safety FSM check ──── unsafe? ─▶ reject
        │                                     ├─ Mount.SlewToCoordinates
        │                                     ├─ (optional) plate-solve & center
        │                                     ├─ FilterWheel + Focuser set
        │                                     ├─ Camera.StartExposure
        │  ◀── live progress (exposure %, mount, dome) ── Telemetry Publisher
        │                                     ├─ readout → FITS on local disk
        │                                     ├─ checksum + provenance headers
        │                                     ├─ local plate-solve → WCS
        │                                     └─ enqueue upload
        ▼                                          │ SFTP/HTTPS upload (async)
Operator console (live)                            ▼
                                          Image/Archive service
                                            ├─ verify checksum
                                            ├─ calibrate (bias/dark/flat)
                                            ├─ refine solve (if needed) + QA metrics
                                            ├─ store FITS in object store
                                            ├─ index metadata in Postgres
                                            └─ generate preview (JPEG/PNG) + thumbnail
```

## 6. Multi-site & multi-instrument modeling

- **Site** = a geographic location (lat/lon/elevation, horizon profile, timezone,
  weather sources).
- **Instrument** = a telescope + its attached devices (mount, cameras, filter wheel,
  focuser, rotator, dome it lives in).
- A site may host **multiple instruments**; a dome may host one or more.
- Capabilities are **declared in config** (e.g., "has rotator: false", "filters:
  [L,R,G,B,Ha,OIII,SII]", "max slew rate", "pier-flip behavior"). The UI and
  scheduler adapt to declared capabilities — no per-site code.

## 7. Deployment

| Tier | Where | How |
|------|-------|-----|
| **Core** | IUB server or small cloud VM | Docker Compose (start) → Kubernetes (scale). VPN hub. |
| **Edge** | Mini-PC at each site | Docker Compose; auto-start on boot; watchdog; offline-capable |
| **Networking** | — | **WireGuard** mesh VPN; each site dials out (no inbound ports needed at the site) |
| **Time** | All nodes | NTP (chrony); PTP if sub-ms needed for fast photometry |
| **CI/CD** | — | Build images centrally; edge nodes pull on a schedule/maintenance window, never auto-update mid-night |

### Why sites dial out
Most remote sites are behind NAT/firewalls with no public IP. WireGuard initiated
from the edge to the core hub means **no inbound firewall holes at the observatory**,
which is both simpler and safer.

## 8. Failure handling summary

| Failure | Behavior |
|---------|----------|
| WAN link down | Site keeps running cached plan (if allowed); buffers data; safety fully local |
| Core down | Sites continue safely; reconnect and sync on recovery |
| Device hangs | Device Manager timeout → mark faulted → Safety FSM may park/close |
| Weather turns unsafe | Safety FSM closes dome, parks mount, aborts exposure — independent of core |
| Power loss | UPS holds long enough to park & close; on restore, system comes up `SAFE`/parked and waits for human clearance |
| Bad command after reconnect | TTL/idempotency rejects stale commands |

See **[02-DEVICE-CONTROL.md](02-DEVICE-CONTROL.md)** for the device abstraction.
