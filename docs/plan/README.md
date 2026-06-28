# CRITO — Coordinated Astronomical Scheduling, Slewing & Acquisition

**A multi-site robotic & manual telescope/dome control system for IUB (Independent University, Bangladesh)**

This folder is the complete design plan for CRITO: a platform to operate multiple
telescopes and domes spread across different geographic sites, from a single
control center at IUB. It supports **full manual control** of every device, plus
optional automation (planning, scheduling, autonomous plan execution), live
telemetry while observing, plate solving, structured data storage, transient-alert
ingestion, and remote retrieval of images over FTP/SFTP.

---

## Document index

| # | Document | What it covers |
|---|----------|----------------|
| 00 | [Overview & Goals](00-OVERVIEW.md) | Vision, scope, actors, requirements, glossary |
| 01 | [System Architecture](01-ARCHITECTURE.md) | Services, message bus, multi-site topology, deployment |
| 02 | [Device Control Layer](02-DEVICE-CONTROL.md) | Mounts, cameras, domes, focusers, filter wheels; ASCOM Alpaca / INDI drivers |
| 03 | [Data Pipeline](03-DATA-PIPELINE.md) | Acquisition, calibration, plate solving, FITS storage, FTP/SFTP transfer |
| 04 | [Planning & Scheduling](04-PLANNING-SCHEDULING.md) | Target planning, observability, scheduler, plan execution engine |
| 05 | [Transient Alert Broker](05-TRANSIENT-BROKER.md) | GCN/Kafka, ZTF/Rubin, TNS, scrapers, filtering, follow-up triggers |
| 06 | [Weather & Safety](06-WEATHER-SAFETY.md) | Weather, seeing, all-sky, interlocks, safe/unsafe state machine |
| 07 | [Database Design](07-DATABASE.md) | Schemas: inventory, scheduling, telemetry (time-series), images, alerts |
| 08 | [Frontend & Real-time UX](08-FRONTEND.md) | Operator console, live updates, manual control panels |
| 09 | [Technology Stack](09-TECH-STACK.md) | Concrete language/library/infra choices and rationale |
| 10 | [Security & Operations](10-SECURITY-OPS.md) | AuthN/Z, networking, secrets, backups, monitoring |
| 11 | [Roadmap](11-ROADMAP.md) | Phased delivery plan with milestones |

---

## The 60-second picture

```
                         ┌──────────────────────────────────────┐
                         │   IUB CONTROL CENTER (operators)       │
                         │   Web console · planning · monitoring  │
                         └───────────────┬──────────────────────┘
                                         │  HTTPS / WSS (VPN)
                         ┌───────────────┴──────────────────────┐
                         │     CRITO CORE  (cloud / on-prem)     │
                         │  API · Scheduler · Broker · DB · MQ   │
                         └───────┬───────────────────┬──────────┘
                                 │  VPN / message bus │
              ┌──────────────────┴───┐         ┌──────┴──────────────────┐
              │   SITE A — Site Agent │         │   SITE B — Site Agent   │
              │  (edge node)          │   ...   │  (edge node)            │
              │  Alpaca/INDI drivers  │         │  Alpaca/INDI drivers    │
              │  ├─ Mount             │         │  ├─ Mount               │
              │  ├─ Camera + filters  │         │  ├─ Camera              │
              │  ├─ Focuser           │         │  ├─ Dome / roll-off     │
              │  ├─ Dome              │         │  ├─ Weather / all-sky   │
              │  └─ Weather / seeing  │         │  └─ Plate-solve worker  │
              └───────────────────────┘         └─────────────────────────┘
```

**Key idea:** each remote site runs a self-sufficient **Site Agent** (edge node) that
can keep a session safe even if the link to IUB drops. The IUB core orchestrates,
stores, plans, and presents. Manual control is *always* available and always takes
priority over automation.

---

## Design principles

1. **Safety first, always interruptible.** Hardware and people are expensive; a
   bad slew or an open dome in rain is unacceptable. Safety interlocks live at the
   edge and cannot be overridden by a lost network link.
2. **Manual control is a first-class mode, not a fallback.** Operators can drive
   any device directly at any time; automation is layered on top and yields to the
   human.
3. **Standard device abstractions.** Build on **ASCOM Alpaca** (cross-platform REST)
   and **INDI** rather than vendor SDKs, so new hardware drops in with little code.
4. **Edge autonomy + central orchestration.** Sites survive disconnects; the core
   coordinates and is the source of truth for plans and archives.
5. **Everything observable.** Every command, state change, and frame is logged and
   streamed live to operators.
6. **Reproducible, FAIR data.** Images carry full provenance in FITS headers and a
   queryable database; raw data is never mutated.

> Start with **[00-OVERVIEW.md](00-OVERVIEW.md)**.
