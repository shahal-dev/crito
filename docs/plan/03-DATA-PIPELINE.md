# 03 — Data Pipeline (acquisition → calibration → plate solving → storage → transfer)

## 1. Overview

```
Camera readout ─▶ Raw FITS (edge) ─▶ checksum + provenance headers
   │                                      │
   │                                      ├─▶ local plate-solve (fast, for pointing)
   │                                      └─▶ spool for upload (resumable SFTP/FTP)
   ▼
Edge local disk (immutable raw)                Core archive ingest
                                                  ├─ verify checksum
                                                  ├─ master-calibration apply (bias/dark/flat)
                                                  ├─ refine plate-solve + WCS
                                                  ├─ QA metrics (FWHM, bkg, #stars, ellipticity)
                                                  ├─ object store (raw + calibrated) + DB index
                                                  └─ previews (JPEG/PNG, thumbnails)
```

Principle: **raw frames are immutable**. Calibration and analysis produce *new*
derived products; the original is never overwritten. Everything is reproducible from
raw + recorded recipe.

## 2. Acquisition & FITS authoring (edge)

When the camera reads out, the Site Agent writes a FITS file with a **complete,
standard header** so the frame is self-describing forever:

- **WCS** (after solve): `CTYPE1/2`, `CRVAL1/2`, `CRPIX1/2`, `CD1_1..CD2_2`.
- **Pointing/time:** `RA`, `DEC`, `OBJCTRA`, `OBJCTDEC`, `DATE-OBS` (UTC, start of
  exposure), `MJD-OBS`, `EXPTIME`, `LST`, `AIRMASS`, `ALT`, `AZ`, `PIERSIDE`.
- **Instrument:** `TELESCOP`, `INSTRUME`, `DETECTOR`, `FILTER`, `GAIN`, `OFFSET`,
  `XBINNING`, `YBINNING`, `CCD-TEMP`, `SET-TEMP`, `PIXSIZE1/2`, `FOCALLEN`, `APTDIA`,
  `FOCUSPOS`, `ROTATORANGLE`.
- **Environment:** `AMBTEMP`, `HUMIDITY`, `DEWPOINT`, `PRESSURE`, `WINDSPD`,
  `SKYTEMP`, `SQM`, `SEEING` (if measured).
- **Provenance/CRITO:** `SITE`, `INSTRMID`, `OBSERVER`, `PROPID`/program, `PLANID`,
  `BLOCKID`, `OBSID` (unique), `IMAGETYP` (LIGHT/DARK/BIAS/FLAT), `CRITOVER`,
  `SWCREATE`, `CHECKSUM`/`DATASUM` (FITS standard checksums).

A unique **`OBSID`** ties the frame to its plan/block/exposure record in the DB.

## 3. Plate solving

Two-stage strategy: **fast at the edge for pointing**, **precise at the core for
science WCS**.

| Solver | Where | Use |
|--------|-------|-----|
| **astrometry.net** (local `solve-field` + index files) | Core (and edge) | Blind solving, robust, no internet needed once index files are installed |
| **ASTAP** | Edge | Very fast local solver with its own star DB; great for quick pointing checks |
| **Local index files** (4100/4200/5000 series, or Gaia-based) | Both | Chosen to match each instrument's field of view; ship to each edge node |

**Edge (pointing):** short exposure → ASTAP/astrometry.net with a hint (approx
RA/Dec from mount, pixel scale from config) → solves in ~1 s → pointing correction.
A hint-based solve is fast and reliable; only fall back to a blind solve if the hint
fails (e.g., way off).

**Core (science):** re-solve (or refine) for an accurate WCS, optionally with
**SCAMP + SExtractor/Source Extractor** for distortion-aware astrometric solutions
when high precision is needed (e.g., astrometry of moving objects). WCS written back
into the FITS header (preserving the raw original).

Solve parameters per instrument live in config: pixel scale range, expected FoV,
downsample factor, search radius around the hint, solver timeout.

## 4. Calibration

Standard CCD/CMOS calibration at the core (recipe recorded in DB):

1. **Bias / dark** subtraction (master dark matched by exposure, temp, gain).
2. **Flat-field** division (master flat matched by filter, optical config).
3. Optional: bad-pixel masking, cosmic-ray rejection (e.g., L.A.Cosmic), overscan.

**Master calibration management:**
- Operators acquire bias/dark/flat sequences (manual or scheduled "calibration plan").
- A **master-frame builder** combines them (sigma-clipped median) into masters,
  versioned and tagged by instrument + parameters + validity window.
- The pipeline auto-selects the best-matching master for each light frame; if none
  matches, the frame is stored calibrated-`false` and flagged.

## 5. Quality assessment (QA)

Computed per calibrated frame and stored as metadata (queryable, plotted in UI):
- **FWHM / HFR** (median star) → seeing/focus quality
- **Background level & gradient**
- **Number of detected stars**, limiting magnitude estimate
- **Star ellipticity** → tracking/guiding/collimation issues
- **Saturation fraction**, **cloud flag** (from star count drop / sky temp)
- **Astrometric residual** (RMS of the WCS fit)

These feed: the UI (good/bad frame badges), the scheduler (re-observe if QA fails),
and notifications (alert operator if quality degrades — clouds, dew, focus drift).

## 6. Storage layout

### Object store (FITS + previews)
S3-compatible (MinIO on-prem, or cloud). Path convention:

```
s3://crito-archive/
  raw/{site}/{instrument}/{utdate}/{obsid}.fits
  calibrated/{site}/{instrument}/{utdate}/{obsid}.cal.fits
  masters/{instrument}/{type}/{master_id}.fits
  previews/{site}/{instrument}/{utdate}/{obsid}.jpg
  thumbs/.../{obsid}.thumb.jpg
```

- **Immutable raw**, write-once; versioning enabled.
- Checksums (`DATASUM`/`CHECKSUM` + an external SHA-256) verified on ingest and on
  every transfer.
- Lifecycle policy: keep raw forever (cheap cold tier after N months); calibrated +
  previews on warm tier.

### Database
Metadata, provenance, QA, WCS summary, and pointers to object-store keys live in
Postgres (see [07-DATABASE.md](07-DATABASE.md)). The DB is the **search index**;
the object store holds bytes.

## 7. Remote transfer (FTP / SFTP) — both directions

The user explicitly wants images retrievable over **FTP**. CRITO uses transfer in
two roles:

### A) Edge → Core ingest (push)
- Site Agent **Data Spooler** queues each raw FITS for upload.
- Transport: **SFTP** (preferred, encrypted) or FTPS; resumable, checksum-verified,
  bandwidth-throttled so uploads don't starve the control link.
- Retries with backoff; survives reconnects; deletes local copy only after the core
  confirms checksum (configurable retention so a local copy can be kept).

### B) Core → Users (pull / retrieve)
- An **SFTP/FTP gateway** exposes the archive to observers for bulk download.
- Backed by the object store via a virtual filesystem; **per-user chroot + RBAC** so
  a PI sees only their program's data.
- Same data also available via the **HTTPS archive API** (search + signed download
  URLs) and the web UI. FTP/SFTP is the bulk/scriptable path; HTTPS is the
  interactive path.

```
Users ──SFTP/FTP──▶  CRITO FTP Gateway  ──▶ object store (read-only, scoped)
Users ──HTTPS────▶  Archive API (search, signed URLs)
```

**Security note:** plain FTP is unencrypted; default to **SFTP** (SSH) or **FTPS**
(TLS). If a downstream tool truly requires plain FTP, restrict it to the VPN.

## 8. Data products summary

| Product | Format | Stored | Notes |
|---------|--------|--------|-------|
| Raw light/dark/bias/flat | FITS | object store + DB index | immutable |
| Master calibration frames | FITS | object store | versioned |
| Calibrated science frame | FITS (+WCS) | object store | derived, reproducible |
| Preview / thumbnail | JPEG/PNG | object store | for UI |
| QA metrics | JSON / DB rows | Postgres | queryable |
| Per-night logs & summary | JSON/PDF | object store + DB | nightly report |

## 9. Optional downstream hooks

CRITO stops at calibrated + plate-solved frames, but exposes hooks for science
pipelines: difference imaging, aperture/PSF photometry, light-curve building,
moving-object linking. These can subscribe to the "frame archived" event on the bus
and run as independent consumers — kept out of the core to keep CRITO focused.

See **[04-PLANNING-SCHEDULING.md](04-PLANNING-SCHEDULING.md)** for how observations
are planned and executed.
