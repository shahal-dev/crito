import { useCallback, useEffect, useRef, useState } from "react";
import { getJSON } from "./api";

// d3-celestial vector star chart. Preferred source is the local vendored copy
// (web/public/celestial — fully offline). If the dev server hasn't started
// serving that folder yet (it returns the SPA index.html instead), we fall back
// to the CDN so the map still renders. Once Vite serves public/, it's offline.
const LOCAL_BASE = "/celestial";
const CDN_BASE = "https://cdn.jsdelivr.net/gh/ofrohn/d3-celestial@master";

/* eslint-disable @typescript-eslint/no-explicit-any */
declare global {
  interface Window {
    Celestial?: any;
    d3?: any;
    __celestialBase?: string;
  }
}

let basePromise: Promise<string> | null = null;

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.async = false; // preserve execution order (d3 → projection → celestial)
    s.onload = () => resolve();
    s.onerror = () => reject(new Error(`failed to load ${src}`));
    document.head.appendChild(s);
  });
}

// Prefer the local vendored copy (offline). If the dev server returns its SPA
// index.html instead (hasn't picked up web/public/celestial yet), use the CDN.
async function pickBase(): Promise<string> {
  try {
    const r = await fetch(`${LOCAL_BASE}/celestial.min.js`);
    const t = await r.text();
    if (r.ok && !/^\s*</.test(t) && t.includes("Celestial")) return LOCAL_BASE;
  } catch {
    /* ignore */
  }
  return CDN_BASE;
}

// celestial.min.js is NOT standalone — it needs global d3 (core) + d3.geo.projection
// loaded first. Load all three from the chosen base, in order.
async function ensureCelestial(): Promise<string> {
  if (window.Celestial) return window.__celestialBase || CDN_BASE;
  if (basePromise) return basePromise;
  basePromise = (async () => {
    const base = await pickBase();
    if (!window.d3) await loadScript(`${base}/lib/d3.min.js`);
    // d3 v3 core already has d3.geo.projection; gate on a PLUGIN projection (aitoff)
    // to ensure the extended-projections plugin (airy/aitoff/…) actually loads.
    if (!window.d3?.geo?.aitoff) await loadScript(`${base}/lib/d3.geo.projection.min.js`);
    if (!window.Celestial) await loadScript(`${base}/celestial.min.js`);
    if (!window.Celestial) throw new Error("Celestial global missing after load");
    window.__celestialBase = base;
    return base;
  })();
  return basePromise;
}

const LAYERS = [
  ["stars", "Stars"],
  ["dsos", "DSOs"],
  ["constellations", "Constellations"],
  ["mw", "Milky Way"],
  ["planets", "Planets"],
  ["graticule", "Graticule"],
  ["horizon", "Horizon"],
] as const;

type LayerKey = (typeof LAYERS)[number][0];

// accurate Sun/Moon/planet markers (drawn from /api/sky/bodies, not d3-celestial's
// approximate glyphs) so a looked-up body's circle lands exactly on the marker.
const BODY_STYLE: Record<string, string> = {
  sun: "#ffd34d", moon: "#cfd6e0", mercury: "#b0a08c", venus: "#e8d9a0",
  mars: "#e0664a", jupiter: "#d9b48f", saturn: "#e6cf9a", uranus: "#9fd6e0", neptune: "#6f8fe0",
};

function toLon(raHours: number): number {
  let ra = raHours * 15;
  if (ra > 180) ra -= 360; // d3-celestial expects longitude in -180..180
  return ra;
}

// Local Sidereal Time (approx), hours — the RA at the zenith for lon (deg, +E).
function lstHours(date: Date, lonDeg: number): number {
  const jd = date.getTime() / 86400000 + 2440587.5;
  const gmst = 18.697374558 + 24.06570982441908 * (jd - 2451545.0);
  return (((gmst + lonDeg / 15) % 24) + 24) % 24;
}

export default function SkyMap({
  scopeRaHours,
  scopeDecDeg,
  targetRaHours,
  targetDecDeg,
  prevRaHours = null,
  prevDecDeg = null,
  recenter = 0,
  lat = null,
  lon = null,
}: {
  scopeRaHours: number | null;
  scopeDecDeg: number | null;
  targetRaHours: number | null;
  targetDecDeg: number | null;
  prevRaHours?: number | null; // the previous looked-up target (dimmed circle)
  prevDecDeg?: number | null;
  recenter?: number; // bump to center the map on the current target
  lat?: number | null; // observer latitude (deg) — draws horizon, opens on the zenith
  lon?: number | null; // observer longitude (deg, +E)
}) {
  const [ready, setReady] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [vis, setVis] = useState<Record<LayerKey, boolean>>({
    stars: true,
    dsos: true,
    constellations: true,
    mw: true,
    planets: true,
    graticule: true,
    horizon: true,
  });
  const scope = useRef<[number, number] | null>(null);
  const target = useRef<[number, number] | null>(null);
  const prev = useRef<[number, number] | null>(null);
  const base = useRef<string>(CDN_BASE);
  const centeredOnce = useRef(false);
  const bodies = useRef<Record<string, { ra_deg: number; dec_deg: number }>>({});
  const showBodies = useRef(true);

  // A full Celestial.redraw() repaints every star/line — calling it on every 2 Hz
  // telemetry frame fights mouse interaction and burns CPU. Coalesce to ~1/sec; the
  // map's own drag/zoom redraws still update the markers via the add() callback.
  const rdTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const rdLast = useRef(0);
  const throttledRedraw = useCallback(() => {
    const C = window.Celestial;
    if (!C) return;
    const wait = 1000 - (performance.now() - rdLast.current);
    if (wait <= 0) {
      rdLast.current = performance.now();
      try {
        C.redraw();
      } catch {
        /* ignore */
      }
    } else if (!rdTimer.current) {
      rdTimer.current = setTimeout(() => {
        rdTimer.current = null;
        rdLast.current = performance.now();
        try {
          window.Celestial?.redraw();
        } catch {
          /* ignore */
        }
      }, wait);
    }
  }, []);

  const buildCfg = (v: Record<LayerKey, boolean>) => ({
    container: "crito-celestial",
    datapath: `${base.current}/data/`,
    projection: "airy",
    interactive: true,
    controls: false,
    background: { fill: "#05070b", stroke: "#243042", opacity: 1 },
    stars: { show: v.stars, limit: 6, colors: true, size: 5, propername: true, propernameLimit: 2.5 },
    dsos: { show: v.dsos, limit: 6 },
    constellations: { show: v.constellations, names: true, lines: true, bounds: false, nametype: "iau" },
    mw: { show: v.mw, style: { fill: "#a9bcd6", opacity: 0.12 } },
    planets: { show: false }, // we draw accurate Sun/Moon/planets ourselves (see redraw)
    lines: {
      graticule: { show: v.graticule, stroke: "#243042", width: 0.6, opacity: 0.8 },
      equatorial: { show: v.graticule, stroke: "#2f6", opacity: 0.2 },
      ecliptic: { show: v.graticule, stroke: "#c79a2e", opacity: 0.3 },
    },
    // observer location → draw the horizon for "now"; opens centered on the zenith
    ...(lat != null && lon != null
      ? {
          geopos: [lat, lon],
          follow: "center",
          horizon: { show: v.horizon, stroke: "#c79a2e", width: 1.3, fill: "#0a0d12", opacity: 0.5 },
          daylight: { show: false },
        }
      : {}),
  });

  const centerZenith = () => {
    if (!ready || lat == null || lon == null || !window.Celestial) return;
    let ra = lstHours(new Date(), lon) * 15;
    if (ra > 180) ra -= 360;
    try {
      window.Celestial.rotate({ center: [ra, lat, 0] });
    } catch {
      /* ignore */
    }
  };

  // when location arrives, re-apply config (for the horizon) and open on the zenith once
  useEffect(() => {
    if (!ready || lat == null || lon == null) return;
    try {
      window.Celestial?.apply(buildCfg(vis));
    } catch {
      /* ignore */
    }
    if (!centeredOnce.current) {
      centeredOnce.current = true;
      centerZenith();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, lat, lon]);

  useEffect(() => {
    let cancelled = false;
    ensureCelestial()
      .then((b) => {
        if (cancelled) return;
        base.current = b;
        const C = window.Celestial;
        if (!C) {
          setErr("sky map library unavailable");
          return;
        }
        try {
          C.display(buildCfg(vis));
          C.add({
            type: "Point",
            callback: () => {},
            redraw: () => {
              if (typeof C.mapProjection !== "function") return;
              const ctx = C.context;
              if (!ctx) return;
              // accurate Sun/Moon/planet markers (same source as the resolver)
              if (showBodies.current) {
                ctx.save();
                ctx.font = "10px ui-monospace, monospace";
                for (const [nm, b] of Object.entries(bodies.current)) {
                  let ra = b.ra_deg;
                  if (ra > 180) ra -= 360;
                  const xy = C.mapProjection([ra, b.dec_deg]);
                  if (!xy || isNaN(xy[0])) continue;
                  ctx.fillStyle = BODY_STYLE[nm] || "#cfd6e0";
                  ctx.beginPath();
                  ctx.arc(xy[0], xy[1], nm === "sun" || nm === "moon" ? 5 : 3, 0, 2 * Math.PI);
                  ctx.fill();
                  ctx.fillStyle = "#9fb0c8";
                  ctx.fillText(nm.charAt(0).toUpperCase() + nm.slice(1), xy[0] + 6, xy[1] + 3);
                }
                ctx.restore();
              }
              // previous looked-up target: dimmed, dashed circle
              if (prev.current) {
                const xy = C.mapProjection(prev.current);
                if (xy && !isNaN(xy[0])) {
                  ctx.save();
                  ctx.strokeStyle = "rgba(226,85,79,0.4)";
                  ctx.lineWidth = 1.5;
                  ctx.setLineDash([4, 3]);
                  ctx.beginPath();
                  ctx.arc(xy[0], xy[1], 10, 0, 2 * Math.PI);
                  ctx.stroke();
                  ctx.restore();
                }
              }
              // current looked-up target: solid red circle
              if (target.current) {
                const xy = C.mapProjection(target.current);
                if (xy && !isNaN(xy[0])) {
                  ctx.save();
                  ctx.strokeStyle = "#e2554f";
                  ctx.lineWidth = 2;
                  ctx.beginPath();
                  ctx.arc(xy[0], xy[1], 11, 0, 2 * Math.PI);
                  ctx.stroke();
                  ctx.restore();
                }
              }
              // current scope/mount position: green box + crosshair
              if (scope.current) {
                const xy = C.mapProjection(scope.current);
                if (xy && !isNaN(xy[0])) {
                  ctx.save();
                  ctx.strokeStyle = "#2f9e5a";
                  ctx.lineWidth = 2;
                  ctx.strokeRect(xy[0] - 9, xy[1] - 9, 18, 18);
                  ctx.beginPath();
                  ctx.moveTo(xy[0] - 14, xy[1]);
                  ctx.lineTo(xy[0] - 9, xy[1]);
                  ctx.moveTo(xy[0] + 9, xy[1]);
                  ctx.lineTo(xy[0] + 14, xy[1]);
                  ctx.stroke();
                  ctx.restore();
                }
              }
            },
          });
          setReady(true);
        } catch (e) {
          setErr(`sky map error: ${e instanceof Error ? e.message : e}`);
        }
      })
      .catch((e) => setErr(`could not load sky map: ${e instanceof Error ? e.message : e}`));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // accurate Sun/Moon/planet positions, refreshed periodically
  useEffect(() => {
    let stop = false;
    const tick = () =>
      getJSON<Record<string, { ra_deg: number; dec_deg: number }>>("/api/sky/bodies")
        .then((d) => {
          if (!stop && d) {
            bodies.current = d;
            try {
              window.Celestial?.redraw();
            } catch {
              /* ignore */
            }
          }
        })
        .catch(() => {});
    tick();
    const t = setInterval(tick, 30000);
    return () => {
      stop = true;
      clearInterval(t);
    };
  }, []);

  // the Planets toggle now controls our accurate body markers
  useEffect(() => {
    showBodies.current = vis.planets;
    try {
      window.Celestial?.redraw();
    } catch {
      /* ignore */
    }
  }, [vis.planets]);

  // live scope/mount position (green box) — throttled redraw
  useEffect(() => {
    if (!ready) return;
    scope.current =
      scopeRaHours == null || scopeDecDeg == null ? null : [toLon(scopeRaHours), scopeDecDeg];
    throttledRedraw();
  }, [ready, scopeRaHours, scopeDecDeg, throttledRedraw]);

  // target marker (red circle) follows the RA/Dec boxes — no auto-center on edit
  useEffect(() => {
    if (!ready) return;
    target.current =
      targetRaHours == null || targetDecDeg == null ? null : [toLon(targetRaHours), targetDecDeg];
    throttledRedraw();
  }, [ready, targetRaHours, targetDecDeg, throttledRedraw]);

  // previous looked-up target (dimmed circle) — keeps the last two on screen
  useEffect(() => {
    if (!ready) return;
    prev.current =
      prevRaHours == null || prevDecDeg == null ? null : [toLon(prevRaHours), prevDecDeg];
    throttledRedraw();
  }, [ready, prevRaHours, prevDecDeg, throttledRedraw]);

  // center the map on the target only when explicitly asked (e.g. after a lookup)
  useEffect(() => {
    if (!ready || !recenter || !target.current) return;
    try {
      window.Celestial?.rotate({ center: [target.current[0], target.current[1], 0] });
    } catch {
      /* ignore */
    }
  }, [ready, recenter]);

  const toggle = (k: LayerKey) => {
    const next = { ...vis, [k]: !vis[k] };
    setVis(next);
    if (ready && window.Celestial) {
      try {
        window.Celestial.apply(buildCfg(next));
      } catch {
        try {
          window.Celestial.display(buildCfg(next));
        } catch {
          /* ignore */
        }
      }
    }
  };

  const center = () => {
    if (!ready || !scope.current || !window.Celestial) return;
    try {
      window.Celestial.rotate({ center: [scope.current[0], scope.current[1], 0] });
    } catch {
      /* ignore */
    }
  };

  return (
    <div className="skymap">
      <div id="crito-celestial" className="skymap-canvas">
        {err && <div className="muted skymap-err">{err}</div>}
      </div>
      <div className="row skymap-opts">
        {LAYERS.map(([k, label]) => (
          <label key={k} className="chk">
            <input type="checkbox" checked={vis[k]} onChange={() => toggle(k)} /> {label}
          </label>
        ))}
        <button className="small" onClick={centerZenith} disabled={!ready || lat == null || lon == null} title="Center on what's overhead now">
          Zenith
        </button>
        <button className="small" onClick={center} disabled={!ready || scope.current == null}>
          Center scope
        </button>
      </div>
    </div>
  );
}
