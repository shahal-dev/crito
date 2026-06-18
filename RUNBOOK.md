# CASSA — Runbook (get it operational, step by step)

Everything we've built so far (Phases 0–1 + runtime device connection), driving
your **real instruments** over INDI. Devices are discovered by **Scan** and bound
to roles from the console — no hardcoded device map, any INDI-supported brand.

### Two machines (who does what)

CASSA runs across **two roles**. They can be the same physical box (everything on
one Linux machine next to the telescope) or two boxes on the same network — the
steps below tell you which role each command belongs to.

| Role | What it is | Runs here | Installs needed |
|------|-----------|-----------|-----------------|
| 🛰️ **EDGE NODE** | The Linux box **physically cabled to the instruments** (mount, cameras, focuser, wheel) | the **INDI server** (`indiserver` + your real drivers) | `indi-full` + vendor driver pkgs (step **1c**) |
| 💻 **WORKSTATION** | Where you sit and operate from (laptop/desktop) | the **backend API** + the **web console** | Python venv + CASSA (step **1a**), Node + web deps (step **1b**) |

> **Single-machine setup?** If the instruments are plugged straight into your
> workstation, that one box is *both* roles — run **all** steps on it and use
> `localhost` for the INDI host.

The two talk over TCP: the workstation's backend connects to the edge node's INDI
server on **port 7624**. Open that port between them (or run WireGuard/VPN for a
remote site).

| Component | Role | Port | URL |
|-----------|------|------|-----|
| Web console | 💻 workstation | 5173 | http://localhost:5173 |
| Backend API | 💻 workstation | 8000 | http://localhost:8000 |
| INDI server | 🛰️ edge node | 7624 | (tcp) |
| SFTP gateway (optional) | 💻 workstation | 2022 / 8082 | sftp://localhost:2022 · http://localhost:8082 |

---

## 0. Prerequisites (check once)

**💻 WORKSTATION** — for the backend + console:
```bash
python3 --version     # need 3.11+   (you have 3.12)
node --version        # need 18+     (you have v24)
```

**🛰️ EDGE NODE** — for the instruments: you need `indiserver` plus the vendor
driver packages for your hardware (installed in step **1c**).

---

## 1. One-time setup

### 1a. Python backend &nbsp;— 💻 WORKSTATION
```bash
cd ~/Desktop/cassa

# create + activate a virtualenv (reuse your existing .cassatom if you have it)
python3 -m venv .venv
source .venv/bin/activate
#   (if reusing the one you already made:  source .cassatom/bin/activate )

# install CASSA and its dependencies
pip install --upgrade pip
pip install -e .
```

### 1b. Web console &nbsp;— 💻 WORKSTATION
```bash
cd ~/Desktop/cassa/web
npm install
cd ..
```

### 1c. INDI drivers &nbsp;— 🛰️ EDGE NODE
Install INDI and the driver packages for your instruments on the box the hardware
is cabled to. The full driver set covers most mounts, cameras, focusers and filter
wheels across brands:
```bash
sudo add-apt-repository ppa:mutlaqja/ppa
sudo apt update
sudo apt install -y indi-full          # all INDI device drivers
sudo usermod -aG dialout $USER         # serial permission — then log out / back in
```
> If `add-apt-repository` is unavailable, enable universe first:
> `sudo add-apt-repository universe && sudo apt update`.

---

## 2. Run it (3 terminals)

> Terminal 1 is on the **🛰️ edge node**; Terminals 2 & 3 are on the **💻
> workstation**. On a single-machine setup all three are just three terminals on
> that one box.

### Terminal 1 &nbsp;— 🛰️ EDGE NODE — INDI server

Cable up first: serial mounts via **EQDIR/USB-serial** (bypass any handset),
cameras/focusers/wheels via **USB**. Then start `indiserver` with the drivers for
**your** hardware, e.g.:
```bash
indiserver -v indi_eqmod indi_toupbase indi_asi_ccd indi_asi_focuser
#            └ list the drivers for the devices you actually have
```
Leave it running. If you're not sure which drivers match your gear, the KStars/Ekos
"Profile Editor" lists driver names by brand — use those names here.

### Terminal 2 &nbsp;— 💻 WORKSTATION — backend API
```bash
cd ~/Desktop/cassa
source .venv/bin/activate         # or: source .cassatom/bin/activate

# point CASSA at the edge node's INDI server.
# single-machine setup? skip these two lines — localhost:7624 is the default.
export CASSA_INDI_HOST=192.168.1.50      # the edge node's address
export CASSA_INDI_PORT=7624

uvicorn cassa.core.app:app --reload --reload-dir cassa --host 0.0.0.0 --port 8000
```
You should see `CASSA core ready — INDI <host>:7624` and `INDI transport up`.
(On first run it creates `data/` with the SQLite archive.) You can also leave the
host unset and set it later from the console (**Connect server**).

### Terminal 3 &nbsp;— 💻 WORKSTATION — web console
```bash
cd ~/Desktop/cassa/web
npm run dev
```
Open the printed URL: **http://localhost:5173**

---

## 3. Drive it from the console &nbsp;— 💻 WORKSTATION (browser)

1. Top of the page shows **INDI connected**. If it shows the wrong server, set the
   **INDI host/port** to the edge node and click **Connect server**.
2. In the **Devices** panel: click **Scan** — every device your INDI server exposes
   appears, with its detected role(s).
3. **Auto-detect & connect all** binds each device to its primary role, or assign
   roles manually. For a **serial mount**, type its port (e.g. `/dev/ttyUSB0`, or a
   stable `/dev/serial/by-id/...` path) in its row before clicking **Connect**.
4. **Mount** panel: enter RA/Dec, click **Slew**, watch it converge; toggle
   tracking, park/unpark.
5. **Focuser & Filter** panel: move the focuser, pick a filter.
6. **Camera** panel: set an Object name + exposure, click **Capture & archive**.
7. The frame appears in the **Archive** grid with a working **FITS ↓** download link.

That's the full Phase-0/1 milestone: connect → slew → capture → provenance FITS →
archive → download. Your selections persist to `data/bindings.json` and reconnect
automatically on restart.

---

## 4. Verify from the command line (optional) &nbsp;— 💻 WORKSTATION
```bash
curl localhost:8000/api/health
curl localhost:8000/api/indi/devices            # discovered devices
curl -X POST localhost:8000/api/devices/autodetect
curl -X POST localhost:8000/api/camera/capture \
  -H 'content-type: application/json' \
  -d '{"seconds":2,"object_name":"M42","image_type":"LIGHT"}'
curl "localhost:8000/api/images?limit=5"        # archive index
```

---

## 5. Retrieve images over SFTP/FTP (optional, needs Docker) &nbsp;— 💻 WORKSTATION
```bash
docker compose -f deploy/docker-compose.yml --profile ftp up -d
# open http://localhost:8082  (admin / cassa-admin), create an SFTP user whose
# home folder maps to /srv/archive, then:
sftp -P 2022 <user>@localhost      # browse raw/  previews/  thumbs/
```

---

## 6. Stop / restart

- Stop the web console / backend: `Ctrl-C` in their terminals.
- Stop the `indiserver` on the edge node: `Ctrl-C` in Terminal 1.
- Stop the SFTP gateway: `docker compose -f deploy/docker-compose.yml --profile ftp down`.
- Restart later: repeat **step 2**. Bindings + archive persist in `data/`.

---

## 7. Troubleshooting

| Symptom | Fix |
|--------|-----|
| Console shows **INDI down** | Edge node's `indiserver` not running, or wrong host/port — start it, set the host/port, then **Scan**. |
| Devices panel empty after Scan | INDI server has no drivers loaded — check Terminal 1 lists the drivers for your hardware. |
| A device is missing after Scan | its driver isn't in the `indiserver` command, or the USB/serial cable isn't enumerated — check `lsusb` / `ls -l /dev/serial/by-id/`. |
| Real mount won't connect | wrong serial port or no `dialout` group — set the port in its row; confirm `ls -l /dev/serial/by-id/`; re-login after `usermod`. |
| `503 mount not connected` on slew/capture | bind the device first (Devices panel). |
| Backend import errors after `git pull` | dependencies changed — `pip install -e .` again. |
| Port already in use (8000/5173/7624) | stop the old process, or change the port (`uvicorn ... --port 8001`, etc.). |

---

## 8. Quick reference (Makefile)
```
make install     # pip install -e .  +  web npm install
make backend     # run the API (http://localhost:8000)
make web         # run the console (http://localhost:5173)
make infra       # postgres/redis/nats/minio (later phases)
```
The INDI server runs on the edge node with your real drivers (step 2, Terminal 1);
point `CASSA_INDI_HOST`/`CASSA_INDI_PORT` at it or set it from the console.

---

## 9. Transient follow-up (ALeRCE → approve → observe)

The backend runs a transient pipeline alongside manual control (no extra process):

```
ALeRCE broker ─▶ ingest ─▶ visibility filter (≥30° alt tonight, IUB Dhaka, moon)
            ─▶ candidate list (grouped by ALeRCE class) ─▶ approve (Queue / Execute)
            ─▶ observation plan ─▶ execution sequencer (slew → expose ×N → archive)
```

The console has three tabs (top-right): **Console** (manual control + archive),
**Candidates** (the review/approval queue), and **Observing** (the execution monitor).

**Operate it:**
1. **Candidates** tab — click **Poll ALeRCE now** (or wait for the 10-min auto-poll).
   Only objects that clear **30° during tonight's astronomical-dark window** appear,
   grouped by ALeRCE class (SN, AGN, …), each with peak altitude, airmass, moon
   separation, observable window (BST) and a score.
2. Review and decide per candidate: **Approve → Queue** (attended), **Approve →
   Execute** (auto-dispatch when observable), or **Reject**. Every decision is
   audited and persists across restarts.
3. **Observing** tab — the queue lists approved blocks. Click **Launch** on a queued
   block; watch the live slew → exposure countdown → progress, with **Pause / Resume
   / Abort**. Frames land in the **Console → Archive** with the candidate's name.
4. **Manual control always wins:** any manual mount/camera/focuser/filter command
   pauses the queue (a *manual override* badge appears). Clear it with **clear
   override** / **Resume** in the Observing tab.

**Key settings** (env, all optional — sensible defaults):

| Var | Default | Meaning |
|-----|---------|---------|
| `CASSA_ALT_MIN_DEG` | `30` | horizon altitude limit (observability tag, not a filter) |
| `CASSA_ALERCE_CLASSIFIER` | `stamp_classifier` | ALeRCE classifier (labels fresh alerts). `""` = plain recent objects, class "unknown" |
| `CASSA_ALERCE_CLASSES` | `SN,AGN,VS` | classes to pull (per-class query). `""` = no class filter |
| `CASSA_ALERCE_PROBABILITY` | `0.4` | min classifier probability — **lower this to see more** |
| `CASSA_ALERCE_LOOKBACK_DAYS` | `7` | only ingest objects active within N days |
| `CASSA_ALERCE_POLL_S` | `600` | broker poll cadence (s) |
| `CASSA_DEFAULT_EXPTIME_S` / `CASSA_DEFAULT_COUNT` | `120` / `5` | default recipe per target |
| `CASSA_AUTO_EXECUTE` | `false` | master switch for unattended auto-dispatch — **keep off until a weather/safety system exists** |

> The pipeline queries the **stamp classifier** per class (so each object gets a real
> SN/AGN/VS label) and **automatically falls back** to a plain recent-objects query if
> that returns nothing — so the feed is never silently empty. If you still see nothing,
> the broker returned zero rows: lower `CASSA_ALERCE_PROBABILITY`, widen
> `CASSA_ALERCE_LOOKBACK_DAYS`, or set `CASSA_ALERCE_CLASSIFIER=` (empty) and re-poll.

> **Approve→Execute vs auto-execute:** *Execute* marks a request `auto`, but it only
> runs unattended when `CASSA_AUTO_EXECUTE=true`. With the default (`false`) it simply
> waits in the queue for a manual **Launch** — the safe default given there's no
> weather/safety automation yet.

**Slack / email approval** (optional, currently dormant): set `CASSA_SLACK_BOT_TOKEN`,
`CASSA_SLACK_APP_TOKEN` (Socket Mode), `CASSA_SLACK_CHANNEL` and/or `CASSA_SMTP_*` +
`CASSA_APPROVE_SECRET` to also post interactive approval cards to Slack / email. Left
unset, the console is the sole approval surface.

**Quick API check:**
```bash
curl localhost:8000/api/transient/night                       # tonight's dark window
curl -X POST localhost:8000/api/transient/poll                # poll ALeRCE now
curl "localhost:8000/api/transient/candidates?group_by=class" # observable candidates
```

---

## 10. Guiding with PHD2

CASSA reads PHD2's live guiding error and plots it (RA + Dec, in pixels) in the
**Auto Guider** panel on the Console, and can start/stop guiding.

### 10a. Install PHD2 &nbsp;— 🛰️ EDGE NODE
```bash
sudo apt install -y phd2
```

### 10b. Set up PHD2 (one-time) &nbsp;— 🛰️ EDGE NODE
1. Launch **PHD2** on the edge node (headless is fine over VNC/X-forwarding).
2. **Connect Equipment** → Camera = your **guide camera via INDI** (INDI Camera,
   host `localhost:7624`, choose the guide device); Mount = **INDI Mount** (same
   `indiserver`, for guide pulses) — or on-camera/ST4 if you guide that way.
3. **Calibrate** once near the celestial equator (Dec ≈ 0): pick a star, run PHD2
   calibration, and confirm the guide pulses move it the right way.
4. **Tools → Enable Server** — PHD2's event server on TCP **4400**. This is what
   CASSA connects to.

### 10c. Point CASSA at PHD2 &nbsp;— 💻 WORKSTATION
PHD2's server runs on the edge node, so set its host (defaults to the INDI host):
```bash
export CASSA_PHD2_HOST=192.168.1.50   # edge node; omit to reuse CASSA_INDI_HOST
export CASSA_PHD2_PORT=4400           # default
```
Restart the backend. The **Auto Guider** panel shows **PHD2 &lt;state&gt;** when connected.

### 10d. Use it &nbsp;— 💻 WORKSTATION (browser)
- The **guiding plot** draws **RA (blue)** and **Dec (orange)** error in pixels, live,
  with RMS underneath. It fills in once PHD2 is looping/guiding on a star.
- **Start guiding / Stop** drive PHD2 directly (PHD2 must already be connected to
  equipment and calibrated).

| Symptom | Fix |
|--------|-----|
| Panel shows **PHD2 disconnected** | PHD2 not running, server not enabled, or wrong host — start PHD2, **Tools → Enable Server**, set `CASSA_PHD2_HOST`. |
| Plot empty but connected | PHD2 isn't guiding/looping yet — select a star and Start guiding. |
| **Start guiding** returns 503 | PHD2 not connected to equipment or not calibrated — do that in PHD2 first. |

---

## 11. Weather & safety

CASSA runs a **safety state machine** that gates unattended/auto operation. States:
**SAFE → WARN → UNSAFE → FAULT**. UNSAFE/FAULT trip immediately (fail-fast); returning
to SAFE needs conditions to hold OK for `CASSA_SAFETY_CLEAR_DELAY_S` (hysteresis).

**On UNSAFE/FAULT** the monitor **aborts the running sequence and parks the mount**.
**Auto-execute requires SAFE**; an attended launch is blocked only when UNSAFE/FAULT.
A banner on the Console shows the state, reasons, and weather; the header shows a pill.

### Feeding weather — three ways
1. **Weather API (default, on)** — CASSA auto-polls a weather API for the site's
   lat/lon and feeds the safety monitor. Provider `CASSA_WEATHER_API`: **`open-meteo`**
   (free, no key — the default), `openweather` (needs `CASSA_WEATHER_API_KEY`), or `""`
   to disable. Cadence `CASSA_WEATHER_POLL_S` (600 s). ⚠ A regional API is **coarse** —
   it won't catch a local cloud/shower over the dome; pair it with an on-site sensor (3).
2. **Push (any source)** — a sensor script POSTs readings directly:
   ```bash
   curl -X POST localhost:8000/api/safety/weather -H 'authorization: Bearer <token>' \
     -H 'content-type: application/json' \
     -d '{"humidity":72,"wind_speed":12,"rain":false,"clouds":20}'
   ```
3. **INDI weather device** — set `CASSA_WEATHER_DEVICE="<label>"` and CASSA reads its
   `WEATHER_STATUS` (Ok/Busy/Alert) + parameters directly. **Takes priority** over the
   API (a real on-site sensor beats a regional forecast).

**No data = UNSAFE** — if all sources are silent/unreachable the state stays UNSAFE,
blocking unattended observing until real conditions arrive.

### Thresholds (env, defaults)
| Var | Default | |
|-----|---------|--|
| `CASSA_SAFETY_ENABLED` | `true` | enforce the FSM |
| `CASSA_SAFETY_STALE_S` | `180` | weather older than this → UNSAFE |
| `CASSA_SAFETY_CLEAR_DELAY_S` | `120` | hold OK this long before SAFE |
| `CASSA_SAFETY_HUMIDITY_WARN` / `_UNSAFE` | `85` / `95` | % |
| `CASSA_SAFETY_WIND_UNSAFE` | `40` | km/h |
| `CASSA_SAFETY_CLOUD_UNSAFE` | `90` | % (if the source reports it) |

### Operator controls (Console banner)
- **Emergency stop** — latches FAULT, aborts + parks. **Clear e-stop** to release.
- **Override safety** — disables enforcement (banner turns to "OVERRIDE ON"). Only when
  attended and you accept the risk; auto-execute then ignores safety. Keep it OFF.

> Until you wire a weather source the state stays **UNSAFE** ("no weather data"). That's
> intentional — it blocks unattended observing. For attended manual control you can either
> push a reading or temporarily **Override** the safety.

---

## 12. Plate-solving & autofocus (ASTAP)

CASSA uses **ASTAP** for both plate-solving (pointing) and HFR autofocus. Install it
on each edge node and point CASSA at it.

### Install on the edge node (Pi/mini-PC)
1. Install the ASTAP CLI (`astap` on PATH, or set `CASSA_ASTAP_PATH`).
2. Install a **star database** — the **H18** (or **D80**) database is a good all-round
   choice; ASTAP finds it automatically in its data dir, or set `CASSA_SOLVE_DB`.
3. Set the **focal length** in `observatory.yaml` →
   `equipment.telescope.focal_length_mm` (and the camera `pixel_size_um`). This lets
   CASSA pass a tight FOV hint so solves are fast. Without it ASTAP auto-detects FOV
   (slower but still works).

Quick check on the Pi:  `astap -f some-frame.fits -r 30`  → should print `PLTSOLVD=T`.

### Plate-solve & center
**Console → Lookup & Target → "Solve & center".** CASSA captures a short frame,
solves it, and if the pointing is off by more than `CASSA_CENTER_TOLERANCE_ARCSEC`
(30″) it **syncs** the mount to the solved position and **re-slews** to target,
iterating up to `CASSA_CENTER_MAX_ITER` times. Progress shows under the button and
in the activity log. In a plan, tick **Center** to insert this step after the slew.

Optional **WCS injection:** `CASSA_SOLVE_SCIENCE_FRAMES=true` solves *every* LIGHT
frame and writes a TAN WCS into its FITS header (slower — one solve per frame).

### Autofocus (HFR V-curve)
**Console → Filter & Focuser → "Autofocus".** CASSA sweeps the focuser
(`CASSA_AF_STEPS` samples, `CASSA_AF_STEP_SIZE` apart), measures median HFR per step
with ASTAP, fits a parabola, and moves to the minimum (final approach always from one
side to absorb backlash). A **V-curve plot** appears live. In a plan, tick
**Autofocus** to insert this step. Needs ≥ `CASSA_AF_MIN_STARS` stars to trust a sample.

> Set `CASSA_SOLVER=none` to disable both (the Center/Autofocus buttons grey out and
> the plan steps are skipped). ASTAP's `-analyse` output format varies by version — if
> HFR reads as "—", check the ASTAP version and the backend log (it prints the raw
> output at debug level).

---

## 13. Calibration frames in a plan

Each exposure set in a plan has a **Type**: **Light / Dark / Flat / Bias** (quick-add
buttons `+ Darks / + Flats / + Bias`). Mix them in one plan or build a calibration-only
plan (leave the target blank → the slew is skipped). CASSA stamps the correct
`IMAGETYP` and skips dithering / WCS-solving on calibration frames.

- **Dark / Bias** — CASSA moves the wheel to the **opaque "dark" filter** first (the
  QHY MiniCam8 has one). Auto-detected by a slot named *dark/blank/opaque*, or set
  `CASSA_DARK_FILTER_SLOT`. Bias is forced to 0 s. *(Still cap the OTA if you have no
  dark filter.)*
- **Flat** — before the first flat the sequence **pauses and prompts** ("set up your
  flat source…"). Set up your panel / twilight sky, then click **Confirm & continue**
  (on the Console or the Observe tab). Confirming also clears any manual hold you set
  while preparing. Flats honor the selected **filter**.

> CASSA controls the *filter, exposure and frame type* — it does not operate a shutter,
> cap or flat panel. Wire a motorized flat/cap on INDI later to automate the flat setup.
