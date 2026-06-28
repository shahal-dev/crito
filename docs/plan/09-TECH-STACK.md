# 09 — Technology Stack

Concrete, opinionated choices. All open-source-first, Linux-native, and chosen so the
astronomy ecosystem (Astropy, astrometry.net, INDI, ASCOM Alpaca) is first-class.

## 1. Languages
| Layer | Language | Why |
|-------|----------|-----|
| **Core services & Site Agent** | **Python 3.12+** (async) | The astronomy ecosystem lives in Python: Astropy, Astroplan, astroquery, ccdproc, photutils, Alpyca (Alpaca client), PyINDI, gcn-kafka. Async (asyncio) fits device I/O. |
| **Performance-critical bits** | **Rust** or C extensions (optional) | Only if a hot path (e.g., high-rate telemetry fan-out, image crunching) needs it. Don't start here. |
| **Frontend** | **TypeScript + React** | Mature, typed, great real-time tooling. |

> Python everywhere keeps one ecosystem, one team skill set, and direct access to the
> astronomy libraries. Reach for Rust only where profiling proves a need.

## 2. Backend frameworks & libraries
| Need | Choice | Notes |
|------|--------|-------|
| Web API | **FastAPI** | Async, OpenAPI, WebSockets, Pydantic validation |
| Async runtime | **asyncio** (+ `anyio`) | Device I/O is I/O-bound |
| ORM / DB access | **SQLAlchemy 2.x** + **Alembic** (migrations) | Mature; async support |
| Validation/models | **Pydantic v2** | Shared schemas across services |
| Task/queue | **NATS JetStream** or **Celery/Redis** (or **Dramatiq**) | NATS preferred (doubles as the edge bus) |
| Device control | **Alpyca** (ASCOM Alpaca client), **PyINDI / indi-client** | Vendor-neutral device layer |
| Astronomy core | **Astropy**, **Astroplan**, **astroquery**, **ccdproc**, **photutils**, **astropy.wcs** | Observability, calibration, WCS, catalogs |
| Plate solving | **astrometry.net** (`solve-field`), **ASTAP**, **SCAMP+SExtractor** | Local solving, no internet dependency |
| Guiding | **PHD2** (external, via its socket API) | Battle-tested autoguider |
| Alerts | **gcn-kafka** (GCN), **alerce_client**, **antares-client**, **lasair** client, **fink-client**, TNS API | Per-source ingest adapters |

## 3. Data & messaging
| Need | Choice |
|------|--------|
| Relational DB | **PostgreSQL 16** + **PostGIS** + **Q3C/pgSphere** |
| Time-series | **TimescaleDB** (Postgres extension) |
| Object store | **MinIO** (S3-compatible, on-prem) or cloud S3 |
| Cache / locks / pub-sub | **Redis** |
| Message bus (edge↔core, events) | **NATS (JetStream)** — lightweight, reconnect-tolerant, durable. **MQTT (EMQX/Mosquitto)** acceptable alternative for pure device telemetry |
| Alert stream consumption | **Kafka** clients (GCN/brokers expose Kafka) |

> **Why NATS over Kafka for the control plane:** Kafka is heavy to run at a tiny edge
> node. NATS JetStream is a single small binary, supports request/reply + durable
> streams, and reconnects cleanly over flaky WAN links. Use Kafka only as a *client*
> to consume external alert streams.

## 4. Frontend stack
| Need | Choice |
|------|--------|
| Framework | **React 18 + TypeScript + Vite** |
| Server state | **TanStack Query** |
| Live state | WebSocket client → **Zustand** store |
| Charts | **uPlot** (dense telemetry), **Plotly** (analysis) |
| Sky charts | **Aladin Lite** |
| FITS/image viewer | **JS9** (or a custom WebGL viewer) |
| UI kit | Headless components + custom dark/red observatory theme |

## 5. File transfer
| Need | Choice |
|------|--------|
| Edge→core ingest | **SFTP** (paramiko/asyncssh) or rsync-over-SSH; resumable, checksummed |
| User retrieval | **SFTP/FTPS gateway** (e.g., **SFTPGo** — S3-backed, per-user, RBAC) + HTTPS signed URLs |

> **SFTPGo** is a strong fit: a single Go binary providing SFTP/FTPS/WebDAV/HTTP with
> per-user virtual folders backed directly by S3/MinIO and fine-grained permissions —
> exactly the "retrieve images over FTP" requirement, done securely.

## 6. Infrastructure & ops
| Need | Choice |
|------|--------|
| Containers | **Docker** + **Docker Compose** (start); **Kubernetes (k3s)** if/when scale demands |
| Edge OS | **Ubuntu Server LTS** (or Debian) on mini-PC/NUC |
| VPN | **WireGuard** (sites dial out to core hub) |
| Time sync | **chrony (NTP)**; **PTP** if sub-ms needed |
| Secrets | **HashiCorp Vault** or SOPS-encrypted; never in git |
| CI/CD | **GitHub Actions** / GitLab CI; build images, run the protocol + pipeline unit/integration tests |
| Observability | **Prometheus** + **Grafana** (metrics), **Loki** (logs), **OpenTelemetry** (traces), **Sentry** (errors) |
| Notifications | Slack/Telegram bots, SMTP email, optional SMS gateway |

## 7. Testing
| Level | Approach |
|-------|----------|
| Unit | pytest; INDI protocol parsing/serialization, device discovery/binding, FITS authoring (no hardware, no server) |
| Integration | Against a real `indiserver` on a bench rig — full plan→execute→solve→archive |
| Plate-solve | Fixture FITS frames with known WCS; assert solved within tolerance |
| Safety | Fault-injection: inject rain/wind/sensor-timeout/link-drop faults → assert dome closes & mount parks |
| Load | Telemetry fan-out + many concurrent WS clients |
| End-to-end | Cypress/Playwright against the running console + backend |

## 8. Repository structure (monorepo suggestion)
```
crito/
  core/                 # FastAPI services (or modular monolith)
    api/  scheduler/  broker/  archive/  realtime/  inventory/  safety/
  agent/                # Site Agent (edge)
    devices/  safety_fsm/  executor/  solver/  spooler/
  dal/                  # device abstraction layer + adapters (alpaca, indi)
  common/               # shared pydantic models, schemas, utils
  web/                  # React frontend
  deploy/               # docker-compose, k8s, wireguard, ansible
  docs/plan/            # ← this plan
  tests/
```

## 9. Build-vs-reuse note
Consider studying / partially reusing mature open systems before building from
scratch:
- **RTS2** (Remote Telescope System 2) — autonomous observatory control.
- **INDI/Ekos (KStars)** — proven device control & sequencing on Linux.
- **OCS** (LCO Observatory Control System) ideas for multi-site scheduling.
- **NINA**/**ACP** (Windows) for feature/UX inspiration.

CRITO's differentiator is the **integrated multi-site core + transient broker + manual
operator console + FTP archive** tailored to IUB; reuse device-layer maturity where it
saves time (e.g., lean on INDI/Alpaca drivers and PHD2 rather than reimplementing).

See **[10-SECURITY-OPS.md](10-SECURITY-OPS.md)** and **[11-ROADMAP.md](11-ROADMAP.md)**.
