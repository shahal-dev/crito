# 12 — Transient Follow-up Pipeline (concrete build plan)

> A focused, **build-now** plan for the transient follow-up loop, layered on the
> *current* SQLite/FastAPI/React app (Phases 0–1, manual INDI control + FITS
> archive). This is the pragmatic single-site realization of the heavier designs in
> [04-PLANNING-SCHEDULING.md](04-PLANNING-SCHEDULING.md) and
> [05-TRANSIENT-BROKER.md](05-TRANSIENT-BROKER.md) — no Kafka, no Postgres, no
> astroplan, no multi-site. Add those later; don't block on them.

> **Status (implemented):** Phases A, B, C, E, F, G are built and tested
> (`tests/test_transient.py`, 21 tests). Phase D (Slack/email approval) is scaffolded
> and wired but **dormant** until tokens/SMTP are configured. The on-sky run (Phase F
> milestone) is verified against a fake device layer offline; the live INDI run is
> pending real hardware. See **[RUNBOOK.md](../../RUNBOOK.md) §9** to operate it.

## Context

The rig is connected and we can already slew, expose, author provenance FITS, and
archive — manually, one frame at a time, from the web console. The goal now is the
**transient follow-up loop** end to end:

```
ALeRCE (broker) ─▶ ingest ─▶ visibility filter (≥30° alt, IUB Dhaka, tonight's dark
window, moon) ─▶ candidate list (grouped by ALeRCE class) ─▶ notify supervisor in
Slack/email ─▶ supervisor approves → Queue OR Execute ─▶ observation request (recipe)
─▶ execution sequencer (slew → expose ×N → archive) ─▶ frames in the archive.
```

### Decisions locked with the operator
- **Approval is interactive in Slack** (and email as a NAT-safe fallback). The
  **Approve** button itself chooses **Queue** vs **Execute now**. The console
  "Candidates" tab mirrors state and is the audit source of truth.
- **Both run modes**: attended queue (operator launches) *and* auto-execute (runs
  when observable). Auto-execute ships **off by default** until a weather/safety FSM
  exists. Manual override always wins.
- **Pull everything recent & observable**, but **group/label by ALeRCE class**
  (Supernova, AGN, QSO, …) so the supervisor triages by class.

### Key facts that shape the design
- `astropy 7.2` (with IERS data) is already installed → the visibility engine needs
  **no new astro dependency**; compute alt/az + twilight + moon with astropy directly.
- No HTTP client present → add **`httpx`** (async) for ALeRCE REST polling. **No
  Kafka**, no pandas `alerce` client.
- The running service needs its **own Slack bot** (`slack_sdk`, Socket Mode — outbound
  WebSocket, works behind IUB NAT, no public URL). It **cannot** use Claude's MCP
  Slack/Gmail tools at runtime — those exist only in a Claude session, not in the
  deployed FastAPI process.
- The imaging path is already fully async: `IndiCamera.capture` awaits an
  `asyncio.Future` resolved by the BLOB callback, so a 120 s exposure **does not block
  the event loop** — the executor can reuse `DeviceManager.capture` and live telemetry
  keeps flowing.

## Architecture

Three background tasks started in `app.py` `lifespan` next to the existing
`_broadcaster`, plus an approval surface — all hung off `app.state` like `dm`,
`archive`, `db` are today.

```
                 ┌──────────────┐
   ALeRCE REST ─▶│ AlertPoller  │─▶ Alert rows ─▶┌──────────────────┐
   (httpx, 10min)└──────────────┘                │ CandidateService │
                                                 │  + visibility.py │ astropy: dark
                                                 └────────┬─────────┘ window, ≥30°,
                                                          ▼            moon, airmass
                                            Candidate rows (grouped by class)
                                                          ▼
                                   ┌────────────────────────────────────┐
                                   │ ApprovalService                    │
                                   │  • SlackBot (slack_sdk Socket Mode)│  buttons:
                                   │  • EmailNotifier (smtplib + HMAC   │  Queue /
                                   │    deep-link)                      │  Execute /
                                   └───────────────┬────────────────────┘  Reject
                       approve(queue|execute)      ▼
                                   ┌────────────────────────────────────┐
                                   │ RequestBuilder → ObservationRequest │ recipe:
                                   │  → ExecutionBlock + ExecutionSteps  │ filter×exp×N
                                   └───────────────┬────────────────────┘
                                                   ▼
                                   ┌────────────────────────────────────┐
   /ws/telemetry  ◀── progress ────│ ExecutionSequencer (single-flight)  │
   (existing WS)                   │  slew → [center/focus stub] →       │ reuses
                                   │  ∀exp: dm.capture → archive.ingest  │ DeviceManager
                                   └────────────────────────────────────┘ + Archive
```

New backend package `cassa/transient/`; new router `cassa/core/transient_routes.py`
mounted via `app.include_router(...)`.

## Data model — `cassa/core/transient_db.py` (registers on the existing `Base`)

Import `Base`/`_utcnow` from `db.py` so `db.init()`'s `create_all` auto-creates these
(import the module in `app.py` lifespan before `db.init()`). String columns backed by
`enum.StrEnum` constants (SQLite-friendly; clean Postgres/Alembic migration later).
Each model gets a `.dict()` like `Image.dict()`.

| Model | Purpose | Key fields |
|-------|---------|-----------|
| `Alert` | raw ALeRCE ingest (audit/reprocess) | `id`(=oid), `source`, `received_utc`, `last_seen_utc`, `ra_deg`, `dec_deg`, `class_label`, `class_prob`, `mag_last`, `ndethist`, `firstmjd`, `lastmjd`, `raw_json` |
| `Candidate` | a visibility-surviving alert for **one night** | `id`(=`oid_utdate`), `alert_id`, `ut_date`, `class_label`, `class_prob`, `ra_deg`, `dec_deg`, `mag`, `state`, `score`, `window_start_utc`, `window_end_utc`, `max_alt_deg`, `min_airmass`, `moon_sep_deg`, `moon_illum_frac`, `notified_at`, `decided_at`, `decided_by`, `request_id` |
| `ObservationRequest` | approved candidate + recipe | `id`(uuid), `candidate_id`, `object_name`, `ra_deg`, `dec_deg`, `recipe_json`, `mode`(`attended\|auto`), `priority`, `state`, `window_start_utc`, `window_end_utc`, `created_by` |
| `ExecutionBlock` | one runnable on-sky unit | `id`, `request_id`, `state`, `seq`, `current_step`, `total_steps`, `n_done`, `n_failed`, `error`, `started_at`, `ended_at` |
| `ExecutionStep` | one step of a block | `id`, `block_id`, `seq`, `kind`(`slew\|center\|autofocus\|filter\|expose`), `params_json`, `state`, `image_id`→`Image.id`, `started_at`, `ended_at`, `error` |
| `AuditEvent` | append-only state-change log | `id`, `ts`, `actor`(`slack:U… \| email:… \| console \| system`), `action`, `entity_type`, `entity_id`, `detail_json`, `result` |

State enums: `CandidateState(new→notified→approved_queue\|approved_execute→rejected\|expired)`,
`RequestState(pending→queued→ready→done\|cancelled)`,
`BlockState(queued→running→paused→done\|failed\|aborted)`,
`StepState(pending→running→done\|failed\|skipped)`.

> `create_all` creates missing tables but never alters them. For dev schema changes to
> transient tables, drop `data/cassa.db` (same as `Image` today); Alembic at the
> Postgres cutover.

## Backend services — `cassa/transient/`

- **`alerce.py` — `AlerceClient`**: async wrapper over the ALeRCE ZTF REST API
  (`https://api.alerce.online/ztf/v1`). `query_objects(...)` hits `GET /objects`
  (params: `firstmjd`/`lastmjd` window, `ndet`, `class`, `probability`, `page_size`,
  order by `lastmjd`), paginates, normalizes each object to the `Alert` shape. Owns the
  shared `httpx.AsyncClient` (created in lifespan, closed in `finally`). Defensive
  normalization (missing field → null, never crash).
- **`poller.py` — `AlertPoller.run(app)`**: background loop every
  `CASSA_ALERCE_POLL_S` (default 600 s). Pull recent window (`CASSA_ALERCE_LOOKBACK_DAYS`),
  upsert `Alert` rows (track high-water `lastmjd` → fetch only deltas), hand new/updated
  alerts to `CandidateService`. Try/except per cycle (like `_broadcaster`/`DeviceManager._run`),
  exponential backoff + jitter on 429/5xx.
- **`visibility.py` — pure functions** (no hardware, unit-testable): the astropy engine
  (below). The key offline-testable seam.
- **`candidates.py` — `CandidateService`**: `evaluate_alert(alert)` → compute visibility
  against tonight's cached dark-window grid; if `max_alt ≥ 30°` inside the dark window,
  upsert a `Candidate` with window/alt/airmass/moon + `score`. `score = w_prob·class_prob
  + w_alt·(max_alt/90) + w_air·(1/min_airmass) + w_moon·(moon_sep/180) − w_faint·(mag/limit)`
  (configurable weights). Owns transitions `approve(action, recipe?)` / `reject()` →
  write `AuditEvent`, call `RequestBuilder`. `list_candidates(ut_date, state, group_by=class)`.
- **`requests.py` — `RequestBuilder`**: `build(candidate, recipe, mode)` →
  `ObservationRequest`, then expand `recipe_json` into ordered `ExecutionBlock` +
  `ExecutionStep` rows (slew → center/autofocus stubs → per-exposure: filter step +
  expose step). Default recipe from config, overridable per ALeRCE class (e.g. mono
  ToupTek luminance `5×120 s`).
- **`approvals.py` — `ApprovalService` + `SlackBot`** and **`email_notify.py` —
  `EmailNotifier`**:
  - `SlackBot`: `slack_sdk` async **Socket Mode** client + `AsyncWebClient`, started only
    when `CASSA_SLACK_BOT_TOKEN`(xoxb) + `CASSA_SLACK_APP_TOKEN`(xapp) are set. Posts a
    Block Kit candidate card to `CASSA_SLACK_CHANNEL` with buttons **Approve→Queue**
    (`approve_queue`), **Approve→Execute now** (`approve_execute`), **Reject** —
    `action_id`/`value` encode `candidate_id`. Listener ACKs immediately, calls
    `CandidateService.approve/reject`, then `chat.update`s the card with the decision +
    actor. Reconnect with backoff; never crashes the app if Slack is down.
  - `EmailNotifier`: stdlib `smtplib`/`email` (zero new dep), `CASSA_SMTP_*`. Sends a
    candidate summary with a **signed deep-link**
    `/api/transient/approve?token=<hmac>` (stdlib `hmac`+`base64` over
    `candidate_id|action|exp`, `CASSA_APPROVE_SECRET`) → the email/NAT-safe approval path.
  - `ApprovalService.notify(candidate)` fans out to Slack + email, sets
    `Candidate.state=notified`.
- **`executor.py` — `ExecutionSequencer.run(app)` + queue** (below).

### Lifespan wiring (`cassa/core/app.py`)
```python
app.state.http = httpx.AsyncClient(timeout=30)
app.state.alerce = AlerceClient(app.state.http, settings)
app.state.notifier = ApprovalService(settings, db.sessionmaker)
app.state.requests = RequestBuilder(settings, db.sessionmaker)
app.state.candidates = CandidateService(settings, observatory, db.sessionmaker,
                                        app.state.requests, app.state.notifier)
app.state.executor = ExecutionSequencer(app)
app.state.poller = AlertPoller(app)
tasks = [asyncio.create_task(_broadcaster(app)),
         asyncio.create_task(app.state.notifier.start()),   # Slack Socket Mode
         asyncio.create_task(app.state.poller.run(app)),
         asyncio.create_task(app.state.executor.run(app))]
# finally: cancel tasks, await notifier.stop(), await http.aclose()
app.include_router(transient_router)
```
Every service degrades gracefully when its config/credentials are absent.

## Visibility engine — `cassa/transient/visibility.py` (astropy only)

`EarthLocation` from `observatory.location` (IUB Dhaka 23.8138 N, 90.4246 E, +6h).
Build once per `ut_date` (cached):
- **Dark window**: sample a `Time` grid sunset→sunrise (bracketing local midnight from
  `utc_offset_hours`); `get_sun(grid).transform_to(AltAz(...))`; dark = contiguous span
  with Sun alt `< −18°` (astronomical). Fallback to `−12°` if −18° never reached (record
  which; guard only — won't trigger at Dhaka).

Per candidate on the dark sub-grid (~5-min steps), vectorized:
- `SkyCoord(ra,dec).transform_to(AltAz(obstime=grid, location=loc))`.
- **Observable mask** = `alt ≥ 30°` AND inside dark window. Window = first/last masked
  time → `window_start/end_utc`; empty mask → not observable (skip / don't persist).
- `max_alt_deg` = max alt in window; `min_airmass` = `1/sin(max_alt)` (reject alt≤0;
  matches `DeviceManager.capture`'s airmass).
- **Moon**: `get_body("moon", grid, loc)`; `moon_sep_deg` at window midpoint;
  `moon_illum_frac` from Sun–Moon elongation. Min-separation (default 30°) as a scoring
  penalty, not a hard cut.

Edge cases handled by the mask itself: far-south never-rises → empty mask; circumpolar
north → window spans the whole dark window; twilight clip via the AND with the dark
mask; ±5-min endpoint tolerance (documented; 30° has margin). All `Time`s tz-aware UTC;
the executor **re-checks observability at dispatch** — never trusts a stale window. Run
a large candidate batch via `asyncio.to_thread` if it ever stalls the loop (>~20 ms).

## Execution engine — `cassa/transient/executor.py`

Single-flight loop (one mount): pick the next runnable block, run to completion, repeat.
- **Attended**: a block runs only when explicitly launched (`/blocks/{id}/launch`).
- **Auto**: dispatch the highest-priority `mode=auto` block whose target is observable
  now + inside dark window + `CASSA_AUTO_EXECUTE` on + `manual_override` clear.

Per block, calling the **same async methods the manual endpoints use**:
1. Guard: `dm.connected`, mount+camera bound, re-check observability now, override clear.
2. **slew**: `await dm.mount.slew_to_radec(ra_deg/15, dec_deg, track=True)`; poll
   `status().slewing` to idle (timeout).
3. **center / autofocus**: stub steps marked `skipped` now (plate-solve/HFR later) —
   present in the schema so they slot in without migration.
4. **per exposure** (`count`): `authored = await dm.capture(exptime_s, "LIGHT",
   object_name, filter_slot)` → `rec = await archive.ingest(authored["fits"],
   authored["meta"])`; link `Step.image_id = rec["id"]`. (Optional dither nudge later.)
5. Completion: `Block.state=done`, `Request.state=done`, `AuditEvent`. On exception:
   `failed`+`error`, audit, continue to next block.

**Live progress**: the sequencer updates `Block.current_step/n_done/total_steps` and an
in-memory `executor.progress`. Extend `_broadcaster` (or `DeviceManager.snapshot()`) to
merge an `executor` block into each `/ws/telemetry` frame — **no new socket**. Control
verbs `pause/resume/abort` set flags checked between steps; `abort` also calls
`dm.mount.abort()`. Any manual device endpoint sets `manual_override=True` → the loop
finishes the current exposure then stops auto-dispatch ("manual override always wins").

## API — `cassa/core/transient_routes.py`

```
GET  /api/transient/candidates?ut_date&state&group_by=class
GET  /api/transient/candidates/{id}
POST /api/transient/candidates/{id}/approve   {action:"queue"|"execute", recipe?}
POST /api/transient/candidates/{id}/reject
GET  /api/transient/approve?token=...          # signed email deep-link approval
GET  /api/transient/alerts?limit               # raw alert debug
POST /api/transient/poll                       # trigger an ALeRCE poll now (testing)
GET  /api/transient/night                      # tonight's dark window / twilight
GET  /api/transient/requests | /queue          # ordered blocks + states
POST /api/transient/blocks/{id}/launch | /pause | /resume | /abort
POST /api/transient/queue/reorder              {block_ids:[...]}
GET  /api/transient/config | POST              # recipe defaults, weights, alt min, auto toggle
```
Telemetry adds: `executor{state,block_id,object,step,n_done,total,exposure_remaining,
manual_override}`, `queue_len`, `auto_execute`. New `CASSA_*` settings in `config.py`:
`ALERCE_POLL_S`, `ALERCE_LOOKBACK_DAYS`, `ALT_MIN_DEG=30`, `SLACK_BOT_TOKEN`,
`SLACK_APP_TOKEN`, `SLACK_CHANNEL`, `SMTP_*`, `APPROVE_SECRET`, `AUTO_EXECUTE=false`.

## Frontend

Turn `App.tsx` into a minimal tabbed shell (`Console | Candidates`), lifting the
existing `/ws/telemetry` subscription into the shell and passing `tel` down (Devices /
CameraCard already take `tel`). New files, pure React/TS, no new deps:
- **`Candidates.tsx`**: collapsible groups by ALeRCE class; each card shows class+prob,
  mag, RA/Dec, tonight's window (UTC + local), max alt, airmass, moon sep, score, state
  badge (reuse `pill ok/warn/bad/idle`); buttons Approve→Queue / Approve→Execute /
  Reject; re-fetch on action + periodic refresh (mirrors Slack/email decisions).
- **`ExecutionMonitor.tsx`**: reads `tel.executor` + `tel.queue_len` — current
  block/object, step "expose 3/5", exposure countdown, progress bar, Pause/Resume/Abort,
  ordered queue with up/down reorder; a "tonight" banner from `/api/transient/night`.
- **`api.ts`**: add `Candidate`/`ObsRequest`/`Block`/`ExecutorTel` types + the new
  `executor`/`queue_len`/`auto_execute` telemetry fields. **`styles.css`**: group +
  progress-bar styles.

## Phased delivery (each independently testable)

| Phase | Scope | Milestone | Needs rig? |
|-------|-------|-----------|-----------|
| **A** | `transient_db.py` models; `AlerceClient` + `AlertPoller`; `GET /alerts`, `POST /poll`; add **`httpx`** | `POST /api/transient/poll` populates `alert` rows from live ALeRCE; unit test vs recorded JSON fixture | No |
| **B** | `visibility.py` + `CandidateService`; `GET /candidates`, `GET /night` | unit tests: far-south → not observable, near-zenith → observable w/ sane window/airmass; poller emits candidates grouped by class | No |
| **C** | tabbed `App.tsx` + `Candidates.tsx`; `/approve` `/reject` + `AuditEvent` | approve/reject in browser; state + audit persist across restart | No |
| **D** | `approvals.py` + `email_notify.py`; Slack Socket Mode bot; signed deep-link; add **`slack_sdk`** | new candidate posts an interactive Slack card; button transitions candidate + updates message; console mirrors; email link works | Slack workspace |
| **E** | `requests.py`; approve → request + block + steps; `GET /queue`, reorder | approving creates a queued block (5×120 s expanded into steps), reorderable; dry-run validation (no mount cmds) | No |
| **F** | `executor.py`; launch/pause/resume/abort; telemetry `executor`; `ExecutionMonitor.tsx` | **headline test**: launch a queued block → slew → loop `dm.capture`+`archive.ingest` → frames in Archive; live countdown/progress over WS; pause/abort; manual preempts | **Yes** |
| **G** | guarded auto-execute (`mode=auto` from Execute-now button) | auto-mode request fires within its window unattended, defers outside it; **ships off by default** until weather/safety FSM | **Yes** |

## New dependencies
`httpx` (Phase A) and `slack_sdk` (Phase D) — add to `pyproject.toml` + `requirements.txt`.
astropy already present. Email uses stdlib. No Kafka/Postgres/astroplan.

## Explicitly deferred (hooks left in place)
Plate-solve "center on target" + autofocus (step kinds exist as stubs), weather/safety
FSM (gates auto-execute — Phase 4 of the master roadmap), GW/neutrino HEALPix tiling,
multi-broker (TNS/ANTARES/Lasair) behind the same `AlertSource` shape, multi-site,
Postgres/Timescale cutover, dithering/cadence monitoring.

## Verification
- **A/B/E**: `pytest` unit tests (ALeRCE fixture normalization; Dhaka visibility on a
  fixed date; recipe→steps expansion) + `curl POST /api/transient/poll` then
  `GET /api/transient/candidates`.
- **C**: open the console Candidates tab, approve/reject, restart backend, confirm state
  persists.
- **D**: configure a Slack app (Socket Mode, xapp+xoxb), confirm a candidate card posts
  and a button click transitions state; trigger the email path and follow the deep-link.
- **F (on-sky)**: bind devices, approve→Queue, launch the block, watch the mount slew and
  frames land in the Archive with the candidate object name, with live progress on
  `/ws/telemetry`; test pause/abort and manual takeover.
