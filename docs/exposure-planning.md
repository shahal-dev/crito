# Exposure-time & SNR planning — manual

How CRITO decides **how long to expose a target, at what gain, and how many subs**
to reach the signal-to-noise the *science* requires — the small-observatory
equivalent of an observatory Exposure Time Calculator (ETC). This document is both
the **theory** (so the numbers mean something) and the **operating manual** (so you
can run it). For the at-the-scope checklist, see **RUNBOOK §14**.

> TL;DR — Define the SNR your science needs → pick a gain → feed the planner your
> measured sensor constants → it returns the sub-exposure length, the number of
> subs, and the total integration time. Measure the constants once with
> `crito.calib.characterize`; everything else is the SNR equation.

## Contents
1. [The two tools](#1-the-two-tools)
2. [The physics: the CCD/CMOS SNR equation](#2-the-physics-the-ccdcmos-snr-equation)
3. [Why CMOS (the IMX585) changes the recipe](#3-why-cmos-the-imx585-changes-the-recipe)
4. [The two sub-exposure bounds](#4-the-two-sub-exposure-bounds)
5. [The decision procedure](#5-the-decision-procedure)
6. [The quantities and how we estimate them](#6-the-quantities-and-how-we-estimate-them)
7. [Measuring the constants (calibration)](#7-measuring-the-constants-calibration)
8. [The calibration table format](#8-the-calibration-table-format)
9. [Tool reference (CLI / API / web)](#9-tool-reference)
10. [Worked examples](#10-worked-examples)
11. [Assumptions & limitations](#11-assumptions--limitations)
12. [Troubleshooting](#12-troubleshooting)
13. [References](#13-references)

---

## 1. The two tools

| Tool | Module | What it does | Hardware? |
|------|--------|--------------|-----------|
| **Characterization** | `crito/calib/` | Measures the camera's real, per-gain constants (read noise, system gain, dark current) and on-sky constants (sky rate, zero point), and writes a calibration table. | Yes (camera over INDI) |
| **Exposure planner** | `crito/transient/exposure.py` | Turns a required SNR into a sub-exposure length, sub count, and total integration. Pure math. | No |

The planner is **usable immediately** with datasheet/estimated constants (an example
table ships). It becomes *trustworthy* once you replace those with measurements from
the characterization tool. The math (`crito/calib/analysis.py`,
`crito/transient/exposure.py`) is pure and unit-tested offline
(`tests/test_calib.py`, `tests/test_exposure.py`).

---

## 2. The physics: the CCD/CMOS SNR equation

Signal-to-noise for a source measured in a photometry aperture of `n_pix` pixels
over an exposure of length `t`:

```
                       S · t
   SNR  =  ───────────────────────────────────────────
            √(  S·t  +  n_pix·( B·t + D·t + R² )  )
```

| symbol | meaning | unit |
|--------|---------|------|
| `S`    | source count rate (all the source's light in the aperture) | e⁻/s |
| `B`    | sky background per pixel | e⁻/s/px |
| `D`    | dark current per pixel | e⁻/s/px |
| `R`    | read noise per pixel (per read) | e⁻ RMS |
| `n_pix`| number of pixels in the aperture | — |
| `t`    | exposure time | s |

### Where each term comes from

- **Signal** = `S·t` electrons collected from the source.
- **Source shot noise** — photon arrivals are Poisson, so the *variance* of `S·t`
  detected electrons is itself `S·t`; the noise is `√(S·t)`.
- **Sky shot noise** — `B·t` sky electrons per pixel across `n_pix` pixels →
  `n_pix·B·t` electrons, Poisson variance `n_pix·B·t`.
- **Dark-current shot noise** — thermal electrons `D·t` per pixel, Poisson →
  variance `n_pix·D·t`.
- **Read noise** — Gaussian electronics noise, `R` e⁻ RMS per pixel *per read*, so
  variance `R²` per pixel → `n_pix·R²` for one read.

Independent noise sources add in **quadrature** (variances sum), which is the sum
under the square root.

### The two regimes (this is the whole game)

- **Background/shot-limited** (long subs, bright or light-polluted sky): the sky
  term dominates the denominator and
  `SNR ≈ S·√t / √(n_pix·B)` → **SNR ∝ √t**. To double SNR you need **4× the time.**
- **Read-noise-limited** (very short subs, faint target, dark sky): the constant
  `n_pix·R²` dominates and `SNR ≈ S·t / (R·√n_pix)` → **SNR ∝ t**. Here SNR climbs
  *faster* than √t — until the sky term takes over.

Knowing which regime you're in tells you what to change: in the shot-limited regime
only **total integration time** helps; in the read-limited regime, longer subs help
disproportionately (which is exactly why you avoid being read-limited — see §4).

### Magnitude error

For photometry, SNR maps directly to a magnitude uncertainty. Since
`m = −2.5·log₁₀(flux) + const`, a small flux error `δF` gives
`σ_mag = (2.5/ln10)·(δF/F) = 1.0857 / SNR`.

| SNR | σ_mag | use |
|----:|------:|-----|
| 5   | 0.22  | 5σ detection |
| 10  | 0.11  | rough photometry |
| 100 | 0.011 | 1 % photometry |
| 300 | 0.004 | high-precision |

### Stacking subs

You almost never take one long exposure; you take `N` subs of length `t` and
combine them. For equal subs the signals and variances both scale with `N`, so:

```
   SNR_total  =  √N · SNR_sub
```

Equivalently, for total integration `T = N·t`, read noise is paid `N` times:
`SNR = S·T / √( S·T + n_pix·(B·T + D·T + N·R²) )`. The planner uses the clean form:
**`N = ⌈(SNR_required / SNR_sub)²⌉`**, then `T = N·t`.

---

## 3. Why CMOS (the IMX585) changes the recipe

On a CCD, read noise `R` is a fixed number. On the QHY miniCAM8 (Sony IMX585) and
modern CMOS sensors, **read noise *and* full well both change with the gain
setting**, because of *dual conversion gain (DCG)*:

- **Low gain (LCG):** read noise ~3–7 e⁻ but the **full ~50 ke⁻ well** → maximum
  dynamic range. For bright stars, photometry across a wide magnitude range, lunar.
- **High gain (HCG, above the DCG step):** read noise drops abruptly to ~**1 e⁻** but
  the full well collapses to ~10 ke⁻ → saturates fast. For faint, low-surface-
  brightness and narrowband targets.
- **Linearity HDR mode** (QHY): merges high+low gain → ~1 e⁻ read noise *and* ~46 ke⁻
  well, 16-bit. Gain/offset are locked; **validate its linearity before trusting it
  for photometry.**

Consequences baked into the workflow:

1. **Gain is a parameter you choose first**, before solving for exposure time.
2. **Every constant is tied to the gain it was measured at.** Read noise and full
   well live *per gain* in the calibration table.
3. **Everything is stored in electrons**, not ADU. Electrons are the physical
   quantity; ADU depend on the gain. The characterization tool converts ADU→e⁻ using
   the measured system gain (e⁻/ADU), so the sky rate and zero point come out
   **gain-independent** and live *per filter*.

> See `[[camera-touptek-g3m662m]]` for this site's two sensors. With ~1 e⁻ read
> noise and Dhaka's bright urban sky, you become sky-limited in *under a second* in
> broadband but only after *tens of seconds to minutes* in narrowband — so
> narrowband + high gain is where this camera does science in the city.

---

## 4. The two sub-exposure bounds

The SNR equation tells you the *total* time. Two practical bounds fix the *sub*
length on a CMOS camera.

### 4a. Sky-limited floor (minimum useful sub)

Make each sub long enough that sky shot noise swamps read noise; otherwise you pay
the read-noise penalty on every frame and stacking is inefficient. Require the
per-pixel sky variance to exceed read variance by a factor `k`:

```
   B·t ≥ k·R²      →      t_min = k · R² / B          (default k = 10)
```

With `k = 10`, read noise adds only `√(1 + 1/k) − 1 ≈ 5 %` to the per-pixel noise —
the point of diminishing returns. Below `t_min`, shorter subs throw away SNR.

### 4b. Saturation ceiling (maximum sub)

The brightest pixel you care about must stay below the (linear) full well. For a
Gaussian PSF of `FWHM` (pixels), a source of total rate `S_bright` has a **peak
pixel rate** of `S_bright / (2π σ²)` with `σ = FWHM / 2.3548`. So:

```
   t_max = fill_frac · FW / ( peak_rate + B + D )     (default fill_frac = 0.7)
```

`fill_frac = 0.7` keeps you inside the linear range (sensors deviate from linear
near saturation). The bright pixel is usually a **field star**, not the (faint)
target — pass its magnitude as `brightest_mag`; otherwise the target protects
itself.

### 4c. When the bounds conflict

If `t_max < t_min`, the field **saturates before sky noise dominates** — you cannot
be both unsaturated and sky-limited. This happens with a bright star in a dark
(narrowband) sky. The planner warns and picks `t_max` (saturation wins; accept a
small read-noise penalty). Remedies: lower the gain (more full well), a smaller
aperture, or simply accept the penalty.

The recommended sub is chosen as `min(t_max, max(t_min, 3·t_min))` — a few× the floor
when there's room, clamped under the ceiling. Override it with `desired_sub_s`, or
cap it (tracking/guiding limit) with `max_sub_s`.

---

## 5. The decision procedure

1. **Science → required SNR.** What does the measurement need?

   | science goal | typical SNR |
   |--------------|-------------|
   | confident detection | 5 (5σ); source-extraction often flags at 3σ |
   | 10 % photometry (±0.1 mag) | 10 |
   | 1 % photometry (±0.011 mag) | 100 |
   | transit / asteroseismology (millimag) | thousands — reached by binning many subs |
   | spectroscopy: redshift / abundances | ~10 / ~100 per resolution element |

2. **Pick a gain / mode** from the dynamic-range-vs-read-noise trade-off (§3).
3. **Gather the constants** at that gain: read noise `R`, full well `FW` (per gain);
   sky `B`, zero point `ZP` (per filter); dark `D` (per temperature); plus optics
   (focal length, pixel size) and seeing.
4. **Sub window:** floor `t_min = k·R²/B`, ceiling `t_max = fill·FW/(peak+B+D)`.
   Pick a sub inside it.
5. **Stack:** `SNR_sub` from the equation → `N = ⌈(SNR_req/SNR_sub)²⌉` →
   `T = N·t_sub`.

The planner does steps 4–5 for you and reports the limiting noise term so you can
see *why*.

---

## 6. The quantities and how we estimate them

| quantity | symbol | formula (in `crito/transient/exposure.py`) |
|----------|--------|--------------------------------------------|
| Plate scale | — | `206.265 · pixel_size_um / focal_length_mm` ["/px] |
| FWHM in pixels | — | `seeing_arcsec / plate_scale` |
| Aperture pixels | `n_pix` | `π · (radius_fwhm · FWHM_px)²`, `radius_fwhm = 1.5` (~99 % of a Gaussian PSF) |
| Source rate | `S` | `10^(−0.4·(mag − ZP))` [e⁻/s] |
| Peak pixel rate | — | `S / (2π σ²)`, `σ = FWHM_px / 2.3548` |
| Sub floor | `t_min` | `k · R² / B`, `k = 10` |
| Sub ceiling | `t_max` | `fill · FW / (peak + B + D)`, `fill = 0.7` |
| Per-sub SNR | — | the CCD equation in §2 |
| Subs needed | `N` | `⌈(SNR_req / SNR_sub)²⌉` |

The **limiting noise term** in the output is whichever of `{S·t, n_pix·B·t,
n_pix·D·t, n_pix·R²}` is largest at the recommended sub — i.e. `source`, `sky`,
`dark`, or `read`.

---

## 7. Measuring the constants (calibration)

All four measurements are pure functions in `crito/calib/analysis.py`; the
characterization tool just captures the frames and calls them.

### 7a. Read noise + system gain — photon transfer (needs flats)

Take **two bias** frames and **two flat** frames at a steady level (~30–60 % of full
well). The two-image (Janesick) method cancels fixed-pattern noise by differencing:

```
   g [e⁻/ADU]  =  signal_adu / ( var_temporal(flats) − var_temporal(bias) )
   R [e⁻]      =  read_noise_adu · g       where read_noise_adu = std(bias1−bias2)/√2
```

`var_temporal(pair) = var(frame1 − frame2)/2`. The denominator is the pure shot
variance in ADU², which (in electrons) equals the signal in electrons — so the ratio
is electrons-per-ADU. **Read noise is best measured from the bias pair directly**,
not from a PTC intercept (the intercept is tiny and noisy). A multi-level PTC fit is
available (`gain_from_ptc`) as a cross-check.

### 7b. Dark current — darks at the operating temperature

`D [e⁻/s/px] = (mean_dark − bias) · g / t`. Better: take darks at several exposure
times and use the **slope** of (signal vs time) (`dark_current_series`) — the slope
removes any residual bias offset. The IMX585 is low/no-amp-glow, so `D` is small;
still measure it at your cooling setpoint.

### 7c. Sky rate — an on-sky frame, per filter

`B [e⁻/s/px] = median(light − bias) · g / t − D`. The **median** rejects stars (a
small bright minority). Measure per filter, at your site, ideally near the moon
phase you'll observe in (sky brightness varies a lot with the moon).

### 7d. Zero point — one star of known magnitude, per filter

`ZP = catalog_mag + 2.5·log₁₀(flux_e_per_s)` — the instrumental magnitude that
yields 1 e⁻/s through the whole system. Measure the star's flux (aperture
photometry, sky-subtracted, in e⁻/s) on a calibrated frame and look up its catalog
magnitude in the matching band.

> Sky rate and zero point are **gain-independent in electrons** — measure them once
> per filter and they apply at any gain.

---

## 8. The calibration table format

A YAML file (default `calibration/minicam8.example.yaml`, override with
`CRITO_CALIBRATION_FILE`). Everything is in **electrons**.

```yaml
camera: QHY miniCAM8
sensor: IMX585
pixel_size_um: 2.9

# per GAIN setting — read noise + full well change with gain on CMOS
gains:
  "0":   { read_noise_e: 5.0, full_well_e: 51000, system_gain_e_per_adu: 0.80 }
  "120": { read_noise_e: 1.1, full_well_e: 13000, system_gain_e_per_adu: 0.16 }

# dark current (e-/s/px) keyed by sensor temperature °C — gain-independent
dark_current_e_per_s:
  "-10": 0.01

# per FILTER, measured on-sky (e-, gain-independent)
filters:
  L:  { sky_e_per_s_per_px: 8.0,  zero_point_e: 21.0 }
  Ha: { sky_e_per_s_per_px: 0.12, zero_point_e: 18.6 }
```

| field | meaning |
|-------|---------|
| `gains.<g>.read_noise_e` | read noise (e⁻) at gain `<g>` |
| `gains.<g>.full_well_e` | linear full well (e⁻) — fill from datasheet or a saturation flat |
| `gains.<g>.system_gain_e_per_adu` | e⁻/ADU (used during characterization; not needed by the planner) |
| `dark_current_e_per_s.<T>` | dark current (e⁻/s/px) at sensor temp `<T>` °C |
| `filters.<f>.sky_e_per_s_per_px` | sky background (e⁻/s/px) through filter `<f>` |
| `filters.<f>.zero_point_e` | magnitude giving 1 e⁻/s through filter `<f>` |

Lookups use the **nearest** characterized gain / temperature, so you don't need an
entry for every setting. The characterization tool writes `read_noise_adu` and
`bias_adu` too (diagnostic).

### 8a. Setups (optical trains)

An observatory can list named **setups** in `observatory.yaml` — each an OTA + camera
(+ reducer/barlow) combination. Selecting one in the Exposure tab auto-fills focal
length, pixel size, and the calibration table, while every field stays editable.

```yaml
setups:
  - id: 200p-minicam8
    name: 200P + miniCAM8 (native f/5)
    focal_length_mm: 1000
    pixel_size_um: 2.9
    camera: QHY MiniCam8
    calibration_file: calibration/minicam8.yaml   # per-setup table (optional)
  - id: 200p-minicam8-reducer
    name: 200P + miniCAM8 + 0.5× reducer
    focal_length_mm: 500                           # the reducer changes only this
    pixel_size_um: 2.9
    calibration_file: calibration/minicam8.yaml
```

| field | meaning |
|-------|---------|
| `id` / `name` | identifier / label shown in the dropdown |
| `focal_length_mm` | effective focal length of the train (after any reducer/barlow) |
| `pixel_size_um` | the camera's pixel pitch |
| `camera` | which camera (informational) |
| `calibration_file` | this setup's calibration table — omit to plan with manual constants |

If no `setups:` are declared, CRITO synthesizes a single default from
`equipment.telescope` + the science camera. A `calibration_file` must be one CRITO
knows about (the global default or a file named by some setup) — the server rejects
anything else, so the client can't load arbitrary paths.

---

## 9. Tool reference

### 9a. `crito.calib.characterize` — capture & measure

```
python -m crito.calib.characterize --device "QHY CCD QHYminiCam8" \
    --gains 0,60,120,200 --offset 30 --temp -10 \
    --flat-exptime 2.0 --dark-exptimes 5,30,120 --out calibration/minicam8.yaml
```

| flag | default | meaning |
|------|---------|---------|
| `--host` / `--port` | `localhost` / `7624` | INDI server |
| `--device` | (required) | INDI camera label (find it with the console's **Scan**) |
| `--sensor` | — | sensor name written to the table |
| `--pixel-size` | `2.9` | µm |
| `--gains` | (required) | comma list of gains to sweep, e.g. `0,60,120,200` |
| `--offset` | `30` | sensor offset (keep the bias histogram off zero) |
| `--temp` | — | cool to this °C and wait before measuring |
| `--flat-exptime` | `0` | flat exposure (s) with a uniform light source; `0` = read noise in ADU only |
| `--bias-exptime` | `0` | bias exposure (s), ≈ minimum |
| `--dark-exptimes` | — | comma list of dark exposures (s), e.g. `5,30,120` |
| `--dark-gain` | first gain | gain at which darks are measured |
| `--frac` | `0.5` | central box fraction analysed (avoids edges) |
| `--out` | stdout | write the calibration YAML here |

Notes: a `--flat-exptime` needs a flat source (panel or twilight) giving ~30–60 %
full well — the tool warns if the level is out of range. `full_well_e` is left blank
(fill from datasheet ~54 ke⁻ low gain, or a saturation flat). Sky rate and zero point
are **not** measured here (they need on-sky frames + a known star — use
`crito.calib.analysis.sky_rate` / `zero_point` and add them under `filters:`).

### 9b. `crito.transient.exposure` — plan (CLI)

Calibration-table mode (resolves constants from `--gain` + `--filter`):
```
python -m crito.transient.exposure --calibration calibration/minicam8.yaml \
    --mag 18.5 --snr 30 --filter L --gain 120 --temp -10 \
    --focal-length 1000 --seeing 3.0
```
Manual-constants mode (no table needed):
```
python -m crito.transient.exposure --mag 15 --snr 100 \
    --read-noise 1.1 --full-well 13000 --sky 8 --zp 21 \
    --focal-length 1000 --seeing 3
```

| flag | default | meaning |
|------|---------|---------|
| `--mag` | (required) | target magnitude |
| `--snr` | (required) | required SNR |
| `--focal-length` | (required) | mm |
| `--seeing` | `3.0` | arcsec FWHM |
| `--pixel-size` | `2.9` | µm |
| `--brightest-mag` | target | brightest field star to protect from saturation |
| `--desired-sub` | — | pin the sub length (s) |
| `--max-sub` | — | cap the sub length (s) — tracking/guiding limit |
| `--calibration` + `--gain` + `--filter` + `--temp` | — | calibration-table mode |
| `--read-noise` `--full-well` `--sky` `--zp` `--dark` | — | manual mode (e⁻ / e⁻ / e⁻·s⁻¹·px⁻¹ / mag / e⁻·s⁻¹·px⁻¹) |
| `--json` | off | emit JSON instead of the text summary |

Library use:
```python
from crito.transient.exposure import Calibration, plan_exposure
plan = Calibration.load("calibration/minicam8.yaml").plan(
    mag=18.5, required_snr=30, filter_name="L", gain=120, temp_c=-10,
    focal_length_mm=1000, seeing_arcsec=3.0)
print(plan.summary())     # human-readable
plan.dict()               # JSON-serializable
```

### 9c. REST API

- `GET /api/tools/setups` → the optical-train setups (auto-fill values):
  `{ setups: [{ id, name, focal_length_mm, pixel_size_um, camera, calibration_file }],
  default_id }`. Synthesized from the configured OTA + science camera if none are
  declared in `observatory.yaml`.
- `GET /api/tools/exposure/calibration[?file=<path>]` → table metadata for the UI:
  `{ available, path, camera, sensor, focal_length_mm, pixel_size_um, gains[],
  filters[], temps[] }`. `file` selects a declared per-setup table (allowlisted
  server-side — only files referenced by a setup or the default are loadable).
- `POST /api/tools/exposure` → an exposure plan. Body (calibration mode):
  ```json
  { "mag": 18.5, "required_snr": 30, "filter": "L", "gain": 120, "temp_c": -10,
    "seeing_arcsec": 3.0, "focal_length_mm": 1000 }
  ```
  Manual mode replaces `gain`/`filter` with `read_noise_e`, `full_well_e`,
  `sky_e_per_s_per_px`, `zero_point` (+ optional `dark_e_per_s_per_px`). Optional:
  `brightest_mag`, `desired_sub_s`, `max_sub_s`, `pixel_size_um`, `calibration_file`
  (allowlisted). `focal_length_mm` falls back to `observatory.yaml`'s
  `equipment.telescope.focal_length_mm`.

The response is the full plan (sub window, recommended sub, `n_subs`,
`total_integration_min`, `snr_achieved`, `mag_error`, `limiting_noise`, `warnings`).

### 9d. Web console — the **Exposure** tab

Start by picking a **Setup** (optical train) — this auto-fills focal length, pixel
size, and the calibration table for that OTA+camera(+reducer) combo. Then set a
target magnitude and required SNR (one-click presets), choose filter/gain/temperature
from the calibration table (or flip to **manual constants**), set seeing, optionally
protect a bright field star or cap the sub length, then **Compute plan**. Every field
stays editable after a setup is applied — **manual inputs are always accepted** (tweak
the focal length for an undeclared reducer, or type sensor constants directly).

Setups come from `observatory.yaml` (`setups:`); see §8a. A setup without a
`calibration_file` plans in manual-constants mode.
The readout shows the sub window, recommended sub, SNR/sub, sub count × length, total
integration, achieved SNR ± mag error, the limiting noise term, and warnings.

---

## 10. Worked examples

Rig: **QHY miniCAM8 (IMX585, 2.9 µm) on a Sky-Watcher 200P (1000 mm, f/5)** at IUB
Dhaka → plate scale **0.60″/px**, FWHM **5 px** at 3″ seeing. All reproducible with
the shipped example table (`calibration/minicam8.example.yaml`, placeholder values).

### EX1 — faint supernova, broadband L, high gain
```
python -m crito.transient.exposure --calibration calibration/minicam8.example.yaml \
  --mag 18.5 --snr 30 --filter L --gain 120 --temp -10 --focal-length 1000 --seeing 3
```
```
sub window      sky-limited ≥ 1.5s,  saturation ≤ 1088.4s
→ recommend     4.5s subs   (SNR/sub 0.6, sky-limited)
→ 2939 subs = 222.3 min total   → SNR 30  (±0.036 mag)
```
Bright urban broadband sky → sky-limited within ~1.5 s, so short subs, *thousands*
of them, ~3.7 h total. This is why a faint transient in a city is expensive in
broadband — and why you'd reach for narrowband or accept a lower SNR.

### EX2 — narrowband Hα target, high gain
```
python -m crito.transient.exposure --calibration calibration/minicam8.example.yaml \
  --mag 16 --snr 50 --filter Ha --gain 120 --temp -10 --focal-length 1000 --seeing 3
```
```
sub window      sky-limited ≥ 100.8s,  saturation ≤ 17679.7s
→ recommend     302.5s subs   (SNR/sub 32.3, sky-limited)
→ 3 subs = 15.1 min total   → SNR 56  (±0.019 mag)
```
Narrowband cuts the sky ~70× → the floor moves to ~100 s, long subs are efficient,
and the high IMX585 Hα QE reaches SNR 50 in **15 minutes**. Narrowband is this rig's
sweet spot in the city.

### EX3 — 1 % photometry of a mag-12 star, low gain
```
python -m crito.transient.exposure --calibration calibration/minicam8.example.yaml \
  --mag 12 --snr 100 --filter L --gain 0 --temp -10 --focal-length 1000 \
  --seeing 3 --brightest-mag 12
```
```
sub window      sky-limited ≥ 31.2s,  saturation ≤ 241.7s
→ recommend     93.8s subs   (SNR/sub 522.0, source-limited)
→ 1 subs = 1.6 min total   → SNR 522  (±0.002 mag)
```
A bright star is **source-limited** — one 94 s sub already gives SNR 522. Low gain
buys dynamic range so the star doesn't saturate; here the limit is the star's own
photons, not the sky.

### EX4 — a bright field star forces a saturation conflict
```
python -m crito.transient.exposure --calibration calibration/minicam8.example.yaml \
  --mag 19 --snr 20 --filter Ha --gain 0 --temp -10 --focal-length 1000 \
  --seeing 3 --brightest-mag 7
```
```
sub window      sky-limited ≥ 2083.3s,  saturation ≤ 23.3s
→ recommend     23.3s subs   (SNR/sub 0.2, read-limited)
⚠ saturation ceiling is below the sky-limited floor: the field saturates before
  sky noise dominates. Lower the gain (more full well) or accept a read-noise penalty.
⚠ total integration is 49.8 h …
```
A mag-7 star saturates in 23 s, but the dark Hα sky needs 2083 s to be sky-limited —
**incompatible**. The planner picks the saturation ceiling, flags it `read-limited`,
and warns. The lesson the warning teaches: with a very bright star in a dark sky you
*cannot* be sky-limited; pick your poison (saturate the star, or pay read noise).

---

## 11. Assumptions & limitations

- **Point sources.** `S` is the target's *total* flux in the aperture. For an
  **extended object** (nebula, galaxy), think in *surface brightness* — feed the
  flux per aperture for the region you care about, and remember the contrast against
  the sky is what matters.
- **Gaussian PSF.** The aperture (`1.5·FWHM` radius → ~99 % enclosed) and the peak-
  pixel/saturation estimate assume a Gaussian PSF. Real PSFs have wings; treat the
  saturation ceiling as approximate and leave margin.
- **No explicit airmass/extinction term.** Atmospheric extinction dims the source
  and brightens/reddens the sky with airmass. Fold it into the **zero point** and
  **sky rate** you measure at the airmass you observe (or measure at several
  airmasses).
- **Sky brightness is not constant.** It rises with the moon, twilight, and airmass.
  The single `sky_e_per_s_per_px` per filter is a representative value — measure for
  the conditions you plan in (e.g. a dark-sky and a moon-up value).
- **Linearity & full well.** Keep below ~70 % full well; computational HDR modes need
  their linearity validated before photometry.
- **Calibration is gain/offset/temperature-specific.** Re-measure (and take matching
  darks/flats/bias) when you change any of them.
- **Rolling shutter (IMX585).** Irrelevant for deep-sky SNR; matters for high-time-
  resolution timing (occultations/transits), which this planner does not model.

---

## 12. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `focal length unknown` (400) | Set `equipment.telescope.focal_length_mm` in `observatory.yaml`, or pass `--focal-length` / `focal_length_mm`. |
| `calibration for gain N is incomplete` | `read_noise_e` / `full_well_e` is null in the table — fill it (datasheet or measure). |
| `filter 'X' not in calibration` | Add an entry under `filters:`, or use manual mode. |
| `flat shot variance ≤ 0` (characterize) | Flats too dim, saturated, or not actually flats — set illumination/exposure to ~30–60 % full well. |
| Web tab says "no calibration table" | `CRITO_CALIBRATION_FILE` points nowhere — the UI falls back to manual constants; point it at your table. |
| Plan recommends absurdly long total time | You're sky-limited on a faint broadband target — use narrowband, a brighter SNR target, more aperture, or accept lower SNR (see EX1/EX4). |
| `saturation ceiling below sky-limited floor` warning | Bright star + dark sky conflict (§4c) — lower the gain, smaller aperture, or accept the read-noise penalty. |
| Read noise from the PTC reads ~0 | Expected — the PTC intercept is dwarfed by signal variance. Use the bias-pair read noise (the tool already does). |
| Numbers look wrong after a gain change | Constants are per-gain — re-characterize, or pick the right `--gain`. |

---

## 13. References

- Howell, *Handbook of CCD Astronomy* — the CCD equation and photometry.
- Janesick, *Photon Transfer (DN → λ)* — the two-image gain/read-noise method.
- R. Glover, *"The Best Sub-Exposure Length"* — the CMOS sky-limited sub argument.
- Merline & Howell (1995), *A Realistic Model for Point-Source SNR* — the noise
  budget behind the CCD equation.
- Observatory ETCs for comparison: ESO ETC, STScI Pandeia (HST/JWST), Gemini ITC.
- This repo: `crito/transient/exposure.py`, `crito/calib/analysis.py`,
  `crito/calib/characterize.py`; tests in `tests/test_exposure.py`,
  `tests/test_calib.py`. Operational checklist: **RUNBOOK §14**.
