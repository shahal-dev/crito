# CRITO — Observatory Control System

Web-controlled, multi-observatory system for **transient follow-up**: ingest from the
**ALeRCE** broker → filter by visibility → supervisor approval → automated imaging on
real INDI instruments, with plate-solving, autofocus, guiding, a weather/safety state
machine, role-based access control, and a provenance FITS archive.

## 📖 Documentation
- **[docs/COOKBOOK.md](docs/COOKBOOK.md)** — the full guide: architecture, configuration,
  and task-by-task recipes for everything CRITO does. **Start here.**
- **[RUNBOOK.md](RUNBOOK.md)** — hands-on operational steps (setup, `indiserver`, PHD2,
  weather/safety, plate-solve, calibration).
- **[docs/plan/](docs/plan/README.md)** — the original system design.

## What works
- **Manual control** — async INDI client (no libindi build); runtime role→device
  binding; mount/camera/focuser/filter/guider from the **Console**.
- **Imaging + archive** — provenance FITS (OBSID, pointing, time, filter, focus,
  `CHECKSUM`/SHA-256); SQLite index + previews; archive browser; SFTP gateway.
- **Transient pipeline** — ALeRCE polling, astropy visibility filter, scored candidates,
  approve → Queue/Execute, execution sequencer.
- **Plate-solve + autofocus** (ASTAP), **PHD2 guiding**, fast offline **name resolution**.
- **Plans** — named/scheduled observation plans, per-filter exposure sets, **calibration
  frames** (dark/flat/bias with dark-filter + flat prompt), repeat/resume.
- **Night planning** — almanac (twilight/astronomical night/moon), observability alerts,
  best-time queue ordering.
- **Weather + safety FSM** (auto weather API), **RBAC** (viewer/observer/operator/admin),
  and a **multi-observatory dashboard** (each site its own backend + config).

## How it works (the logic)

### Broker fetching — ALeRCE
- **Source:** ALeRCE ZTF API, `https://api.alerce.online/ztf/v1/objects/`.
- **Strategy:** query the **`stamp_classifier`** once per transient class
  (`CRITO_ALERCE_CLASSES`, default `SN,AGN,VS`) **concurrently**, so every object comes
  back already labelled with a class + probability. If the classifier query returns
  nothing, it falls back to a plain "recent objects" query.
- **Server-side narrowing** (so the broker doesn't sort its whole catalogue):
  `classifier=stamp_classifier`, `class=<cls>`, `probability ≥ CRITO_ALERCE_PROBABILITY`
  (0.4), `ndet ≥ CRITO_ALERCE_MIN_NDET`, `order_by=lastmjd DESC`, and a **`lastmjd`
  lower bound** of `now − CRITO_ALERCE_LOOKBACK_DAYS` (7 days) — i.e. only objects that
  were **active in the last week**. Paged `page_size`×`max_pages` (100×2).
- **Interval:** the poller runs every **`CRITO_ALERCE_POLL_S`** (default **600 s / 10 min**,
  floor 30 s); plus a manual **Poll ALeRCE now** button. Uses one shared async `httpx`
  client with `follow_redirects` + a generous read timeout (`CRITO_ALERCE_TIMEOUT_S`).

### The data we get
Each raw object is normalized (missing keys → `null`, never raises) to:
`oid`, `ra_deg` (meanra), `dec_deg` (meandec), `class_label`, `class_prob`, `mag_last`,
`ndethist`, `firstmjd`, `lastmjd` — and the **full raw item is preserved** so a broker
schema change never loses data.

### Visibility filtering — "observable tonight"
For each alert with coordinates (computed in a worker thread so the feed never stalls):
1. **Tonight's dark window** is built once per night: the Sun's altitude is sampled over
   a local-noon→noon grid (5-min steps), and the **astronomical-dark** run (Sun < **−18°**,
   falling back to −12° then 0° near the poles) is the observing window. Moon/Sun
   positions are pre-computed over that grid.
2. The target's **altitude** is transformed (astropy AltAz) across the dark grid.
   **Observable = it clears `CRITO_ALT_MIN_DEG` (30°) at some point during dark.**
3. When observable, CRITO records the **window** (first→last sample above 30°), the
   **peak altitude**, **min airmass** (`1/sin(alt)`), the **closest moon separation**, and
   the **mean lunar illumination** over the window.

### Best time to observe
`observability()` extends the above with the **≥ 60° window** (the "best" time) and the
**transit time** (peak altitude). This drives the lookup **observability alert** and the
**best-time ordering** of the night queue. (All windows are clipped to the dark window —
they're genuinely usable time, not just "above the horizon".)

### Scoring (figure of merit)
Observable candidates are ranked by a weighted score:
```
score =  w_prob·prob  +  w_alt·(peak_alt/90)  +  w_airmass·(1/min_airmass)
       +  w_moon·(moon_sep/180)  −  w_faint·(mag/mag_limit)
```
(weights `CRITO_SCORE_W_*`, defaults 1·1·0.5·0.5·0.5). Higher = better-placed, brighter,
farther from the Moon, higher-confidence.

### How we save it
One **`Candidate`** row per object **per night** (`id = oid_utdate`) in SQLite. A candidate
is written for **every** alert with coordinates — observable or not — tagged accordingly
(the observability flag is simply "a window was found"). Each row carries the class,
probability, RA/Dec, magnitude, **window / peak alt / airmass / moon sep / illumination**,
score, state, and decision audit. The raw `Alert` is stored separately with its full JSON.

### How we update it
Every poll is an **upsert**: visibility + score are **recomputed and refreshed** on each
pass (the sky moves), but a human **decision is never downgraded** (an approved/rejected
candidate keeps its state). When the **night rolls over**, the `ut_date` changes → new
candidate IDs → the whole feed is re-evaluated against the new dark window. New
*observable* candidates can trigger a Slack/email notification (optional).

### How we show it
- **Candidates tab** lists everything **observable-first, then by score**, each tagged
  *observable* / *not up tonight* with class, magnitude, peak altitude, window, and moon
  separation. It refreshes every **15 s**.
- The **night Queue** re-derives each queued block's observability and orders by **best
  time** (scheduled items by their start time; the rest by their ≥60° window).
- Live device/executor/safety/precision state streams over the **telemetry WebSocket at
  2 Hz**.

### Weather API → safety
- **`WeatherApiPoller`** polls **Open-Meteo** (free, no key — default) for the site's own
  lat/lon every **`CRITO_WEATHER_POLL_S`** (600 s), fetching temperature, humidity, wind
  (km/h), cloud cover, precipitation/rain. (OpenWeatherMap supported with a key; an INDI
  weather device takes priority.) Readings are pushed to the **safety monitor**.
- The **`SafetyMonitor`** re-evaluates **every 1 s**: rain / high wind / high humidity /
  high cloud / **stale (> `CRITO_SAFETY_STALE_S`) or missing data** → UNSAFE; thresholds
  in between → WARN; e-stop → FAULT. Returning to SAFE requires conditions to hold for
  `CRITO_SAFETY_CLEAR_DELAY_S` (hysteresis). UNSAFE/FAULT **aborts the sequence + parks**.
  Live weather shows on the dashboard cards and the Console safety banner.

> **Cadence summary:** ALeRCE poll **600 s** · weather poll **600 s** · safety eval **1 s** ·
> telemetry **2 Hz** · almanac cache 300 s · sky-bodies cache 30 s · night window
> recomputed at roll-over · frontend refresh 15 s (candidates) / 30 s (queue) / 60 s (almanac).

## Architecture (Phase 0)

```
 Browser (React)  ──HTTP/WS──▶  Core API (FastAPI)
                                   │  in-process
                                   ▼
                              Site Agent / DeviceManager
                                   │  async INDI (XML, TCP 7624)
                                   ▼
                       indiserver  (real device drivers, edge node)
```

Agent + Core run in **one process** for Phase 0 (modular monolith). The NATS message
bus that separates them arrives in Phase 5 when there's a real remote site.

## Prerequisites

- Python 3.11+
- Node 18+ (for the web console)
- An `indiserver` running your real device drivers, reachable over TCP (port 7624).
  This usually runs on the **observatory edge node** next to the hardware.

## Run it (3 terminals)

### 1. Start the INDI server (on the edge node, with your real drivers)
```bash
# On the machine the instruments are wired to (sudo apt install indi-bin + the
# vendor driver packages), run indiserver with your device drivers, e.g.:
indiserver -v indi_eqmod indi_toupbase indi_asi_ccd   # whatever you have
```
Point CRITO at it with `CRITO_INDI_HOST`/`CRITO_INDI_PORT` (or set the host/port
from the console). If the instruments are on the same box, `localhost:7624` works.

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

Open **http://localhost:5173**. You should see `INDI connected`.

**Connect your devices from the console** (no YAML editing):
1. In the **Devices** panel, click **Scan** to list whatever the INDI server exposes.
2. Click **Auto-detect & connect all** — each device binds to its role (mount,
   camera, focuser, filter). Or assign roles manually and click **Connect**.
3. For a **real serial mount** (EQ6-R via EQDIR), type the port (e.g. `/dev/ttyUSB0`)
   in its row before connecting.
4. To point CRITO at a **remote edge node**, set the INDI host/port and click
   **Connect server**.

Your choices persist to `data/bindings.json` and reconnect automatically on restart.
Once bound, the Mount/Camera/Focuser panels go live: slew, capture, and archive.

> Order doesn't matter — the backend retries the INDI connection and the console
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
# open http://localhost:8082 (admin / crito-admin), create an SFTP user whose
# home maps to /srv/archive, then:
sftp -P 2022 <user>@localhost      # browse raw/ previews/ thumbs/
```

## Multi-device / multi-brand notes

CRITO makes no assumptions about device brands. Whatever your `indiserver`
exposes shows up under **Scan**; assign each to a role and connect. Serial mounts
(e.g. EQ6-R via EQDIR) take a port like `/dev/ttyUSB0` in their row before
connecting. See the **bring-up checklist** in
[`docs/plan/11-ROADMAP.md`](docs/plan/11-ROADMAP.md).

## Layout
```
crito/
  dal/        device abstraction layer (roles, INDI client, INDI adapter, imaging)
  agent/      site agent (device manager)
  core/       FastAPI app + config
web/          React + TypeScript console
deploy/       docker-compose (supporting infra + SFTP gateway)
docs/plan/    full system design
```
