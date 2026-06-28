# 05 — Transient Alert Broker & Scraper

Goal: continuously ingest astronomical transient/multimessenger alerts, filter them
to what *our* telescopes can usefully follow up, and turn the survivors into
follow-up observation requests (ToO) — either auto-scheduled or operator-approved.

## 1. Alert sources to ingest

| Source | What | Protocol / access |
|--------|------|-------------------|
| **GCN (General Coordinates Network)** | GRBs, gravitational-wave events (LIGO/Virgo/KAGRA), neutrinos (IceCube), gamma-ray (Fermi, Swift), multimessenger | **Kafka** stream (GCN Notices/Circulars over Kafka; classic VOEvent/socket also exists). Auth via GCN client credentials. |
| **ZTF alert stream** | Optical transients, ~real-time | Via community **brokers** (see below) rather than raw Kafka |
| **Vera C. Rubin / LSST** | Massive optical alert stream (when operational) | Via brokers (Rubin designates community brokers) |
| **Community brokers** — **ALeRCE, ANTARES, Lasair, Fink, AMPEL** | Value-added, classified, filterable ZTF/Rubin alerts | REST APIs + Kafka topics + web; each has a Python client |
| **TNS (Transient Name Server)** | Official IAU registry (SN, etc.) — names, classifications, discovery | REST API (bot credentials), bulk feeds |
| **ATels / AstroNotes** | Human-written notices | Scraping / RSS |
| **Minor Planet Center (MPC)** | NEO/asteroid alerts & ephemerides | Web service / scraping |
| **VOEvent network** | Legacy standard event packets | VOEvent brokers (e.g., 4 Pi Sky / Comet) |

> **Design for plug-in sources.** Each source is an **ingest adapter** behind a
> common `AlertSource` interface, so adding a new stream is configuration + a small
> adapter, not a rewrite.

## 2. Ingest architecture

```
   GCN Kafka ─┐
   ALeRCE ────┤
   ANTARES ───┤   ┌───────────────┐   ┌──────────────┐   ┌─────────────────┐
   Lasair ────┼──▶│ Ingest adapters│─▶│ Normalizer    │─▶│ Alert event bus  │
   Fink ──────┤   │ (per source)   │  │ → canonical    │  │ (NATS/Kafka)     │
   TNS API ───┤   └───────────────┘   │   Alert schema │  └────────┬────────┘
   VOEvent ───┘                       └──────────────┘            │
   ATel/MPC scraper                                                ▼
                                                        ┌────────────────────┐
                                                        │ Filter/score engine │
                                                        │ (user-defined rules)│
                                                        └─────────┬──────────┘
                                                                  ▼
                                          de-dup & cross-match ──▶ Candidate store (DB)
                                                                  ▼
                                              observable now? ──▶ ToO request → Scheduler
                                                                  ▼
                                                       notify operator (Slack/Telegram/email)
```

### Canonical Alert schema (normalized)
```
alert_id, source, source_event_id, received_utc, event_utc,
type (GRB|GW|SN|neutrino|kilonova|nova|NEO|unknown),
ra, dec, error_radius (or localization map ref for GW/neutrino),
magnitude, mag_band, classification, classification_prob,
discovery_utc, redshift?, host?, urls[], raw_packet (jsonb)
```

GW/neutrino events have **sky localization maps** (HEALPix/MOC) rather than a point —
store the map and compute the observable high-probability region per site.

## 3. Scraper component

For sources without a clean API (ATels, some MPC pages, observatory bulletins):
- Scheduled scrapers (respecting robots.txt & rate limits) parse pages/RSS, extract
  structured fields, normalize to the canonical schema, and push to the bus.
- Scrapers are isolated, sandboxed jobs with retry/backoff and change-detection so a
  page redesign fails loudly rather than silently ingesting garbage.

## 4. Filtering & scoring (the important part)

Raw streams are huge; CRITO must surface only the few alerts worth follow-up. A
**rules engine** evaluates each normalized alert with user-defined filters:

- **Observability**: is the region above the horizon / airmass limit at any of our
  sites within its useful window? (uses the observability engine)
- **Brightness**: within our instruments' magnitude reach.
- **Type/priority**: e.g., "all GW O4 alerts", "GRBs < 6 h old", "SN candidates with
  classifier prob > 0.8 within 100 Mpc", "NEOs with impact-relevant flags".
- **De-duplication & cross-match**: same physical event across sources is merged;
  cross-match against TNS / known objects to avoid re-discovering catalogued things.
- **Score**: maps to the scheduler's figure-of-merit boost.

Filters are stored, named, versioned, and toggleable per program. Astronomers can
build/edit filters in the UI without code.

## 5. From alert to observation

Qualifying candidates create a **ToO observation request** (see
[04-PLANNING-SCHEDULING.md](04-PLANNING-SCHEDULING.md) §5):
- Suggested instrument config by alert type (e.g., GW counterpart search → wide field
  tiling over the localization map; GRB afterglow → deep multi-band; SN → photometric
  monitoring cadence).
- Policy per program: **auto-execute** (fully autonomous, for time-critical, vetted
  filters) or **propose & page operator** for one-click approval.
- For GW/neutrino maps: generate a **tiling plan** covering the highest-probability
  region given each site's field of view, ranked by probability × observability.

## 6. Outbound / reporting (optional, later)
- Post follow-up results back to the community: TNS reports (classification/photometry),
  GCN Circulars, ATels — with human approval. Keeps CRITO a good citizen of the
  transient ecosystem.

## 7. Reliability
- Durable consumers (Kafka offsets / JetStream) so no alert is lost on restart.
- Every raw packet archived (jsonb) for audit and re-processing when filters change.
- Backpressure: if downstream is slow, buffer and shed only the lowest-priority,
  with logging — never silently drop a GW alert.

See **[06-WEATHER-SAFETY.md](06-WEATHER-SAFETY.md)** for environmental safety.
