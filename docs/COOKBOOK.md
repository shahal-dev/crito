# CRITO Cookbook

End-to-end documentation for operating the CRITO observatory control system: what it
is, how it's wired, how to configure it, and task-by-task recipes for everything it
does. For the bare terminal runbook (one-time setup, starting `indiserver`, SFTP, the
Makefile) see **[RUNBOOK.md](../RUNBOOK.md)** — this cookbook is the conceptual + how-to
companion.

---

## Contents
1. [What CRITO is](#1-what-crito-is)
2. [Architecture](#2-architecture)
3. [Core concepts](#3-core-concepts)
4. [Install & run](#4-install--run)
5. [Configuration](#5-configuration)
6. [The web app (pages)](#6-the-web-app-pages)
7. [Recipes](#7-recipes)
8. [API reference](#8-api-reference)
9. [Configuration reference (`CRITO_*`)](#9-configuration-reference-crito_)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. What CRITO is

CRITO is a web-controlled astronomical observatory system for **transient follow-up**.
It ingests alerts from the **ALeRCE** broker, filters them by **visibility** from your
site, lets a supervisor **approve** targets, and **executes** imaging sequences on the
telescope — with plate-solving, autofocus, guiding, a weather/safety state machine, a
full FITS archive, and role-based access control. It supports **multiple observatories**,
each with its own hardware, surfaced on one dashboard.

The stack: **FastAPI** (async Python) backend speaking the raw **INDI** protocol to the
hardware; **React + TypeScript** (Vite) frontend; **SQLite** (async) for the archive and
pipeline state; **astropy** for all astronomy; **ASTAP** for plate-solving/HFR.

---

## 2. Architecture

CRITO uses an **edge-per-site** model. Each observatory runs its own backend next to its
hardware; one web app aggregates them.

```
                            ┌─────────────── your laptop ───────────────┐
                            │  Browser → CRITO web app (Vite/React)      │
                            │  reads web/public/sites.json, then talks   │
                            │  directly to each site's backend (CORS+JWT)│
                            └───────┬─────────────────────────┬──────────┘
                                    │                         │
                 ┌──────────────────▼─────────┐   ┌───────────▼────────────────┐
                 │  Site A edge node (Pi)      │   │  Site B edge node (Pi)      │
                 │  CRITO backend  :8000        │   │  CRITO backend  :8001        │
                 │   observatory.yaml           │   │   observatory-ciao.yaml      │
                 │   SQLite archive + state     │   │   SQLite archive + state     │
                 │       │                       │   │       │                       │
                 │   indiserver :7624            │   │   indiserver :7625            │
                 │   mount / cameras / focuser   │   │   mount / cameras / focuser   │
                 └──────────────────────────────┘   └──────────────────────────────┘
```

Key consequences:
- **`indiserver` must be local to the hardware** (it's cabled to the gear). The CRITO
  backend connects to it over TCP — usually on the same edge node.
- **One login works across all sites** because every backend validates the same
  **`CRITO_AUTH_SECRET`**-signed JWT. Users live on the hub; site backends only verify.
- The browser talks to each backend **directly** (cross-origin is allowed). Selecting a
  telescope points all subsequent API/WebSocket/image calls at that site's backend.

### Backend components (wired in `crito/core/app.py` lifespan)
| Component | Role |
|---|---|
| `DeviceManager` | INDI transport + runtime role→device bindings |
| `ArchiveService` / `LocalStore` | FITS archive (DB rows + files on disk) |
| `AlertPoller` / `AlerceClient` | poll ALeRCE on a cadence |
| `CandidateService` | visibility-filter + score alerts → candidates |
| `RequestBuilder` | approved candidate → observation request + execution block |
| `ExecutionSequencer` | run blocks on-sky (slew→center→autofocus→expose) |
| `PlanService` | saved/scheduled observation plans |
| `PrecisionOps` | plate-solve centering + HFR autofocus (ASTAP) |
| `SafetyMonitor` | weather + safety state machine |
| `WeatherApiPoller` | auto-feed weather from Open-Meteo / OpenWeatherMap |
| `PHD2Client` | guiding telemetry/control |
| `AuthService` | users, password hashing, JWT |

---

## 3. Core concepts

- **Site / observatory** — one physical location, described by one `observatory.yaml`,
  served by one backend. Has a geographic location, weather, and one or more telescopes.
- **Telescope** — a rig at a site, fronted by its own INDI server (`host:port`). The
  dashboard "Operate" button connects the site backend to the chosen telescope.
- **Role binding** — CRITO doesn't hardcode device names. It discovers whatever INDI
  exposes; you bind each **role** (mount / camera / guide / focuser / filter) to a real
  device in the console. Bindings persist to `bindings.json`.
- **Candidate** — a broker alert that survived the visibility filter for a given night
  (`id = oid_utdate`). Carries observability (window, peak altitude, moon separation).
- **Observation request + execution block** — created when a candidate (or plan) is
  approved/run. The **block** is the executable unit (an ordered list of steps); the
  **executor** runs one block at a time.
- **Roles (RBAC):** `viewer` < `observer` < `operator` < `admin`.
  - viewer — read everything.
  - observer — + create/edit plans, approve candidates, poll ALeRCE.
  - operator — + drive hardware, run/launch, queue management, safety controls.
  - admin — + manage users.

---

## 4. Install & run

> Full terminal walkthrough (venv, `indiserver`, drivers) is in **RUNBOOK §0–§2**. Summary:

**Backend (per site / edge node):**
```bash
cd ~/Desktop/crito
python -m venv .cassatom && source .cassatom/bin/activate
pip install -e .                                  # or: pip install -r requirements.txt
cp .env.example .env                              # then edit (see §5)
indiserver -v indi_simulator_telescope ...        # the site's INDI server (or real drivers)
uvicorn crito.core.app:app --host 0.0.0.0 --port 8000 --reload
```

**Frontend (your laptop):**
```bash
cd web
npm install
npm run dev          # http://localhost:5173  (dev server handles SPA routing)
```

First login: **`admin` / `admin`** (or `CRITO_ADMIN_PASSWORD`). **Change it immediately.**

To run a **second observatory** on the same machine, see [Recipe: add an observatory](#76-add-an-observatory).

---

## 5. Configuration

Three places, in priority order (env always wins):

### 5.1 `observatory.yaml` — the site definition
Identity, **location** (pushed to the mount so RA/Dec are correct), telescopes (each with
its INDI endpoint), equipment (documents the rig + feeds FITS provenance + plate-solve
FOV via `telescope.focal_length_mm` and `camera.pixel_size_um`), `status`, and a manual
`weather.seeing`. Select a different file with `CRITO_OBSERVATORY_FILE`.

### 5.2 `web/public/sites.json` — the locations registry
The list the dashboard aggregates. One entry per site backend URL (`""` = same-origin):
```json
[
  { "id": "iub-rooftop", "name": "IUB Rooftop Observatory", "url": "" },
  { "id": "ciao", "name": "CRITO IUB Astronomical Observatory (CIAO)", "url": "http://localhost:8001" }
]
```

### 5.3 `.env` (or `CRITO_*` env vars) — everything else
API keys, auth, safety thresholds, solver, ALeRCE, etc. The backend auto-loads `.env`
from the directory you run `uvicorn` from. **Rule:** the env var is `CRITO_` + the field
name upper-cased (`weather_api_key` → `CRITO_WEATHER_API_KEY`). Full table in [§9](#9-configuration-reference-crito_).
`.env` is gitignored — keep secrets here.

> **Multi-site:** every site backend must share the same **`CRITO_AUTH_SECRET`** so one
> login works everywhere. Give each site its own `CRITO_DB_URL` and `CRITO_BINDINGS_PATH`.

---

## 6. The web app (pages)

Real per-page URLs (react-router). The **navbar only appears after you select a telescope**.

| URL | Page | Who |
|---|---|---|
| `/` | **Dashboard** — location cards (status, weather, moon/night, telescopes) | viewer+ |
| `/console` | **Console** — clocks + almanac, sky map, mount/lookup/filter/focuser, cameras, guiding, archive | viewer+ (controls: operator) |
| `/candidates` | **Candidates** — ALeRCE alerts, observability, approve/queue/execute | observer+ |
| `/plan` | **Plan** — build/schedule/run observation plans | observer+ |
| `/observe` | **Observe** — current execution, the night Queue, Plans | viewer+ (control: operator) |
| `/users` | **Users** — manage accounts | admin |

The selected telescope persists (localStorage), so reloading a deep link restores it.

---

## 7. Recipes

### 7.0 Select a location & telescope
Dashboard (`/`) → a location card → **Operate** on a telescope. CRITO points the backend
at that telescope's INDI server (`POST /api/indi/server`) and opens the Console. The
nav (Console/Candidates/Plan/Observe) now appears.

### 7.1 Connect the hardware (bind device roles)
Console → **Devices**: **Scan** lists everything INDI exposes; **Bind** each role
(mount/camera/guide/focuser/filter) to a real device. Bindings persist. The mount gets
your site location pushed automatically so RA/Dec are correct.
`POST /api/devices/autodetect` binds each device to its primary role in one click.

### 7.2 Manual control
Console:
- **Mount** — Track on/off, Slew/Sync (via the Lookup box), Park/Unpark, Home, Abort.
- **Lookup & Target** — type a name → resolves to RA/Dec (instant from the local
  catalog; solar-system bodies computed live; Sesame as network fallback). Shows an
  **observability alert** (see 7.10). Then **Slew**, **Sync**, or **Solve & center**.
- **Filter & Focuser** — pick a filter; nudge the focuser (±100 / absolute) or run
  **Autofocus**.
- **Cameras** — expose the science + guide cameras; previews update live.
Any manual command **preempts the execution queue** (manual override) until you resume.

### 7.3 Plate-solve & autofocus (ASTAP)
Prereqs: install **ASTAP** + a star DB on the edge node; set `telescope.focal_length_mm`
in `observatory.yaml`. Then:
- **Solve & center** (Lookup box) — capture → solve → if off by > `CRITO_CENTER_TOLERANCE_ARCSEC`,
  sync the mount and re-slew; iterate. Optional per-frame WCS: `CRITO_SOLVE_SCIENCE_FRAMES=true`.
- **Autofocus** (Focuser box) — HFR V-curve sweep, parabola fit, backlash-compensated
  move; a live V-curve plot shows on the Console.
Full setup in **RUNBOOK §12**. Disable both with `CRITO_SOLVER=none`.

### 7.4 Guiding (PHD2)
Run PHD2 on the edge node with its server enabled; point CRITO at it
(`CRITO_PHD2_HOST/PORT`). Console → **Auto Guider**: Start/Stop; a live RA/Dec error plot
streams from PHD2. Details in **RUNBOOK §10**.

### 7.5 Transient follow-up pipeline
1. **Ingest** — Candidates tab → **Poll ALeRCE now** (or the automatic poll every
   `CRITO_ALERCE_POLL_S`). Tune `CRITO_ALERCE_CLASSES`, `CRITO_ALERCE_PROBABILITY`.
2. **Review** — candidates are shown observable-first, tagged **observable** / **not up
   tonight**, with class, magnitude, peak altitude, window, moon separation.
3. **Decide** — per candidate set **Exp (s)**, **Shots**, optional **Start time**, then:
   - **Execute now** — start exposing immediately.
   - **Queue @ time** — run at the start time you set.
   - **Queue** — add with no time → ordered by best observable time.
   - **Reject** / **↺ Reset** (re-open a decided one).
4. **Observe** — the block flows to the **Queue** (Observe tab) and runs (or you Launch
   it). See **RUNBOOK §9** for the end-to-end detail.

### 7.6 The night Queue (Observe tab)
The Queue enriches each block with tonight's observability:
- Each row shows recipe (`shots×exp`), **⭐ best window (≥60°)**, **🔭 up window (≥30°)**,
  peak altitude, moon separation, frames done.
- **↕ Sort by best time** — re-order by best observable time (scheduled items by their
  start time; the rest by their ≥60° window; non-observable last).
- **↑ / ↓** — manual reorder; the order **persists** and is the dispatch order.
- **Launch** (run now) · **Remove / Abort** (drops it; re-opens its candidate).

### 7.7 Observation plans
Plan tab — reusable named templates:
- **Target** — manual RA/Dec, name lookup, or "From queue". Shows the observability alert.
- **Exposure sets** — rows of Type (**Light / Dark / Flat / Bias**), Filter, Exp, Count,
  Bin, Dither. Quick-add `+ Darks / + Flats / + Bias`. **Repeat** the whole recipe.
- **Autofocus at start** / **Center at start** checkboxes.
- **Run now** · **Resume** (skip completed shots) · **Save for later** · **⏰ Schedule**
  (pick a date/time → fires automatically, gated by safety).

**Calibration behavior** (see also **RUNBOOK §13**):
- **Dark / Bias** — CRITO moves the wheel to the opaque **dark filter** (auto-detected
  by a slot named dark/blank/opaque, or `CRITO_DARK_FILTER_SLOT`). Bias is forced to 0 s.
- **Flat** — the sequence **pauses and prompts**; set up your flat source, then click
  **Confirm & continue** (banner on Console + Observe).

### 7.8 Weather & safety
A **SAFE → WARN → UNSAFE → FAULT** state machine gates unattended operation. Fed by the
**weather API** (Open-Meteo by default, your site's lat/lon — auto), pushed readings
(`POST /api/safety/weather`), or an INDI weather device. **No data = UNSAFE.** On UNSAFE/
FAULT it **aborts the sequence + parks the mount**. Console banner shows state + readings
+ **Emergency stop / Override**. **Auto-execute requires SAFE.** Full reference: **RUNBOOK §11**.

### 7.9 Night almanac
Console clock box shows, with icons and in the site timezone: sun altitude, **sunset/
sunrise**, **civil/nautical/astronomical** twilight, the **astronomical-night** window,
and **moon phase + illumination + rise/set**. Endpoint: `GET /api/sky/almanac`. The
dashboard cards show a compact moon + dark-window line per site.

### 7.10 Observability alert
After a lookup (Console or Plan), an alert shows whether the target is **observable
tonight** (≥30° during the dark window), **from when to when**, and the **best time**
(≥60°), plus peak altitude and moon separation. Endpoint:
`GET /api/sky/visibility?ra_hours=&dec_deg=`.

### 7.11 Manage users (admin)
Users tab — list/create/delete accounts and set roles. Created users can log into **any**
site (shared secret). Change the seeded admin password first.

### 7.12 <a id="76-add-an-observatory"></a>Add an observatory
1. **Config** — write `observatory-<id>.yaml` (identity, location, telescopes with their
   INDI endpoint, equipment + optics).
2. **Backend** — run a backend for it with its own config/DB/bindings/port. Example
   `start-ciao.sh`:
   ```bash
   CRITO_OBSERVATORY_FILE=observatory-ciao.yaml \
   CRITO_DB_URL="sqlite+aiosqlite:///data/ciao.db" \
   CRITO_BINDINGS_PATH="data/ciao_bindings.json" \
   CRITO_INDI_PORT=7625 \
   uvicorn crito.core.app:app --port 8001        # same CRITO_AUTH_SECRET as the others
   ```
3. **Registry** — add it to `web/public/sites.json` with the backend URL.
4. Reload — the dashboard shows the new location card.

In production the backend runs on **that site's own edge node**, and `sites.json` points
at that host instead of `localhost`.

---

## 8. API reference

All under `/api`. **Auth:** send `Authorization: Bearer <token>` (from `POST /api/auth/login`);
images/WS pass `?token=`. Public: `/api/health`, `/api/auth/login`. RBAC: GET → viewer;
writes → operator; planning writes (plans/candidates/poll) → observer; `/api/auth/users*`
→ admin.

**Auth & system**
```
POST /api/auth/login                 GET  /api/auth/me
GET/POST /api/auth/users   DELETE /api/auth/users/{id}   POST /api/auth/users/{id}/password
GET  /api/health   /api/status   /api/observatory   /api/site   /api/activity
WS   /ws/telemetry?token=…           (2 Hz device + executor + safety + precision snapshot)
```
**Devices & manual control**
```
GET  /api/indi/devices    POST /api/indi/rescan    POST /api/indi/server
POST /api/devices/bind | unbind | autodetect
POST /api/mount/{slew,sync,track,park,unpark,home,set-home,set-park,abort}
POST /api/focuser/{move,rel,autofocus}    POST /api/filter/{set,name}
POST /api/camera/{expose,capture}   GET /api/camera/last-image.png
POST /api/center                    GET /api/precision
POST /api/guiding/{start,stop}      GET /api/guiding/graph
```
**Sky / almanac**
```
GET /api/resolve?name=…    /api/sky/bodies   /api/sky/almanac
GET /api/sky/visibility?ra_hours=&dec_deg=
```
**Safety**
```
GET  /api/safety    POST /api/safety/{weather,estop,clear,override}
```
**Transient pipeline**
```
POST /api/transient/poll            GET /api/transient/{alerts,candidates,night,requests,queue}
POST /api/transient/candidates/{id}/{approve,reject,reset}
GET/POST /api/transient/plans   GET/DELETE /api/transient/plans/{id}   POST …/plans/{id}/run
POST /api/transient/executor/{launch,pause,resume,abort,confirm,override}
GET  /api/transient/tonight          POST /api/transient/queue/{reorder,sort}   DELETE …/queue/{id}
```
**Archive**
```
GET /api/images   /api/images/{id}   /api/images/{id}/{fits,preview.png,thumb.png}
```

---

## 9. Configuration reference (`CRITO_*`)

Defaults in parentheses. Site identity/location/optics are normally set in
`observatory.yaml`; an env var overrides the file.

**INDI / identity**
`INDI_HOST` (localhost) · `INDI_PORT` (7624) · `OBSERVATORY_FILE` (observatory.yaml) ·
`SITE_ID` · `OBSERVER` · `INSTRUMENT_ID`

**Storage**
`DB_URL` (sqlite+aiosqlite:///data/crito.db) · `DATA_DIR` (data/store) ·
`BINDINGS_PATH` (data/bindings.json)

**Auth**
`AUTH_SECRET` (change-me) — **same on all sites** · `ADMIN_USER` (admin) · `ADMIN_PASSWORD` (admin)

**Weather & safety**
`SAFETY_ENABLED` (true) · `WEATHER_API` (open-meteo | openweather | "") · `WEATHER_API_KEY` ·
`WEATHER_POLL_S` (600) · `WEATHER_DEVICE` (INDI label) · `SAFETY_STALE_S` (180) ·
`SAFETY_CLEAR_DELAY_S` (120) · `SAFETY_HUMIDITY_WARN` (85) · `SAFETY_HUMIDITY_UNSAFE` (95) ·
`SAFETY_WIND_UNSAFE` (40 km/h) · `SAFETY_CLOUD_UNSAFE` (90 %)

**Plate-solve & autofocus**
`SOLVER` (astap | none) · `ASTAP_PATH` (astap) · `SOLVE_DB` · `ASTAP_SEARCH_RADIUS_DEG` (30) ·
`ASTAP_DOWNSAMPLE` (0) · `SOLVE_EXPOSURE_S` (4) · `CENTER_TOLERANCE_ARCSEC` (30) ·
`CENTER_MAX_ITER` (3) · `SOLVE_SCIENCE_FRAMES` (false) · `FOCAL_LENGTH_MM` · `PIXEL_SIZE_UM` ·
`AF_EXPOSURE_S` (3) · `AF_STEP_SIZE` (100) · `AF_STEPS` (9) · `AF_BACKLASH` (200) ·
`AF_MIN_STARS` (5) · `AF_MIN_SNR` (30) · `DARK_FILTER_SLOT` (0 = auto)

**Guiding** `PHD2_HOST` ("" = INDI host) · `PHD2_PORT` (4400)

**ALeRCE & visibility**
`ALERCE_POLL_S` (600) · `ALERCE_LOOKBACK_DAYS` (7) · `ALERCE_CLASSIFIER` (stamp_classifier) ·
`ALERCE_CLASSES` (SN,AGN,VS) · `ALERCE_PROBABILITY` (0.4) · `ALERCE_TIMEOUT_S` (60) ·
`ALT_MIN_DEG` (30) · `MOON_SEP_MIN_DEG` (30) · `MAG_LIMIT` (18.5)

**Recipe defaults** `DEFAULT_EXPTIME_S` (120) · `DEFAULT_COUNT` (5) · `DEFAULT_FILTER_SLOT`

**Execution** `AUTO_EXECUTE` (false) — guarded auto-dispatch; keep off until safety is trusted

**Approval (optional)** `SLACK_BOT_TOKEN` · `SLACK_APP_TOKEN` · `SLACK_CHANNEL` ·
`SMTP_HOST/PORT/USER/PASSWORD/FROM/TO` · `APPROVE_SECRET` · `API_BASE_URL`

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Dashboard card "unreachable" | That site's backend isn't running, or its `CRITO_AUTH_SECRET` differs (401). Start it; match the secret. |
| New API route 404s | Backend not restarted after an update. Restart `uvicorn`. |
| Login fails | Wrong creds, or `CRITO_AUTH_SECRET` changed (old tokens invalid → log in again). |
| Everything 401 | Token missing/expired — the app should redirect to login; otherwise clear localStorage. |
| RA/Dec wrong by hours | Site `location` not set → mount assumes longitude 0. Fill `observatory.yaml` location. |
| "INDI down" | `indiserver` not running / wrong `CRITO_INDI_HOST:PORT` / device not bound. |
| Safety stuck **UNSAFE "no weather data"** | No weather source reachable. The API needs internet; or push a reading; or **Override** (attended only). |
| Plate-solve / autofocus disabled | `CRITO_SOLVER=none`, ASTAP not installed, or no focuser bound. Install ASTAP + set focal length. |
| HFR reads "—" | ASTAP `-analyse` output format differs by version; check the backend debug log. |
| Tonight's Queue empty | Nothing queued/scheduled yet, or all targets below 30° tonight (winter objects in summer). |
| Candidate buttons all disabled | It's decided with no block to remove → click **↺ Reset**. |
| Deep links 404 in production | `BrowserRouter` needs SPA fallback to `index.html` on your static host (dev/`vite preview` handle it). |

---

*Generated for CRITO. Keep this in sync as features land; the per-task terminal steps live
in [RUNBOOK.md](../RUNBOOK.md).*
