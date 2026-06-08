# CASSA — Phases 0–1

Multi-site telescope/dome control system, built against a fully **simulated
telescope** you can drive by hand from a browser — no real hardware needed yet.

The full design lives in [`docs/plan/`](docs/plan/README.md). This README is the
runnable slice.

## What works

**Phase 0 — foundations & manual control**
- A pure-Python **async INDI client** (`cassa/dal/indi/protocol.py`) — same code path
  for the simulators and, later, the real EQ6-R (`indi_eqmod`) + ToupTek (`indi_toupbase`).
- **DAL roles**: `Mount`, `Camera`, `Focuser`, `FilterWheel` (`cassa/dal/`).
- **Site Agent** (`cassa/agent/`) with a resilient INDI connection + live previews.
- **Core API** + **web console**: live telemetry, slew/park/abort, expose.

**Phase 1 — imaging pipeline & archive**
- **Full-frame capture** that authors a provenance-rich **FITS** (OBSID, pointing,
  time, instrument, filter, focus + FITS `CHECKSUM`/`DATASUM` + SHA-256) — `cassa/agent/fits_writer.py`.
- **Archive**: local object store + SQLite index (`cassa/core/{storage,db,archive}.py`),
  auto previews + thumbnails.
- **Archive API + browser**: search recent frames, view thumbnails, **download FITS**.
- **Focuser + filter-wheel** manual control; filter recorded into FITS headers.
- **SFTP/FTP download gateway** (SFTPGo) over the archive for bulk retrieval.

> **Milestone:** capture a target on the (simulated) camera → a provenance FITS lands
> in the archive → download it over HTTPS or SFTP.

## Architecture (Phase 0)

```
 Browser (React)  ──HTTP/WS──▶  Core API (FastAPI)
                                   │  in-process
                                   ▼
                              Site Agent / DeviceManager
                                   │  async INDI (XML, TCP 7624)
                                   ▼
                              indiserver  (simulator drivers)
```

Agent + Core run in **one process** for Phase 0 (modular monolith). The NATS message
bus that separates them arrives in Phase 5 when there's a real remote site.

## Prerequisites

- Python 3.11+
- Node 18+ (for the web console)
- Docker (easiest way to run the INDI simulator) **or** a local `indiserver`

## Run it (3 terminals)

### 1. Start the INDI simulator
```bash
make indi                 # docker: builds & runs indiserver on :7624
# — or, with INDI installed locally (sudo apt install indi-bin):
indiserver -v indi_simulator_telescope indi_simulator_ccd
```

### 2. Start the core API
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .          # or: pip install -r requirements.txt
make backend              # uvicorn on http://localhost:8000
```

### 3. Start the web console
```bash
cd web && npm install
npm run dev               # http://localhost:5173
```

Open **http://localhost:5173**. You should see `live` + `INDI connected`. Enter an
RA/Dec (defaults point near Orion's Belt), click **Slew** and watch RA/Dec converge;
set an exposure and click **Expose** to get a simulated frame.

> Order doesn't matter — the backend retries the INDI connection, and the console
> reconnects the WebSocket automatically.

## Quick API check (no browser)
```bash
curl localhost:8000/api/status
curl -X POST localhost:8000/api/mount/slew \
  -H 'content-type: application/json' \
  -d '{"ra_hours": 5.59, "dec_deg": -5.39, "track": true}'
# capture a provenance FITS and archive it
curl -X POST localhost:8000/api/camera/capture \
  -H 'content-type: application/json' \
  -d '{"seconds": 2, "object_name": "M42", "image_type": "LIGHT"}'
# list the archive, then download a frame's FITS
curl localhost:8000/api/images?limit=5
curl -OJ localhost:8000/api/images/<image_id>/fits
```

## Retrieve images over SFTP/FTP
```bash
docker compose -f deploy/docker-compose.yml --profile ftp up -d
# open http://localhost:8082 (admin / cassa-admin), create an SFTP user whose
# home maps to /srv/archive, then:
sftp -P 2022 <user>@localhost      # browse raw/ previews/ thumbs/
```

## Cutover to real hardware (Phase 1)

No code change — edit [`sites/virtual.yaml`](sites/virtual.yaml): point `indi.host`
at the edge node and set the real device names (`EQMod Mount`, the ToupTek device
name). Run the real drivers on the edge node instead of the simulators. See the
**bring-up checklist** in [`docs/plan/11-ROADMAP.md`](docs/plan/11-ROADMAP.md).

## Layout
```
cassa/
  dal/        device abstraction layer (roles, INDI client, INDI adapter, imaging)
  agent/      site agent (device manager)
  core/       FastAPI app + config
web/          React + TypeScript console
deploy/       docker-compose + INDI simulator image
sites/        site config (virtual.yaml)
docs/plan/    full system design
```
