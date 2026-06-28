import { useCallback, useEffect, useRef, useState } from "react";
import { ActivityEvent, ResolveResult, Telemetry, getJSON, post } from "./api";
import Almanac from "./Almanac";
import AutofocusPlot from "./AutofocusPlot";
import VisibilityAlert from "./VisibilityAlert";
import CameraCard from "./CameraCard";
import ConfirmBanner from "./ConfirmBanner";
import GuidingPlot from "./GuidingPlot";
import SafetyBanner from "./SafetyBanner";
import SkyMap from "./SkyMap";

function fmt(n: number | null | undefined, d = 2): string {
  return n === null || n === undefined ? "—" : n.toFixed(d);
}

// Local Sidereal Time (approx), hours, from UTC date + east longitude (deg).
function lstHours(date: Date, lonDeg: number): number {
  const jd = date.getTime() / 86400000 + 2440587.5;
  const d = jd - 2451545.0;
  const gmst = 18.697374558 + 24.06570982441908 * d; // hours
  return (((gmst + lonDeg / 15) % 24) + 24) % 24;
}

function hms(h: number | null): string {
  if (h === null || isNaN(h)) return "—";
  const t = ((h % 24) + 24) % 24;
  const hh = Math.floor(t);
  const mm = Math.floor((t - hh) * 60);
  const ss = Math.floor(((t - hh) * 60 - mm) * 60);
  return `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
}

const KIND_CLASS: Record<string, string> = {
  cmd: "k-cmd",
  exec: "k-exec",
  alert: "k-alert",
  error: "k-error",
  info: "k-info",
};

export default function LiveControl({
  tel,
  onCapture,
}: {
  tel: Telemetry | null;
  onCapture?: () => void;
}) {
  const [err, setErr] = useState<string | null>(null);
  const [ra, setRa] = useState("");
  const [dec, setDec] = useState("");
  const [target, setTarget] = useState("");
  const [resolving, setResolving] = useState(false);
  const [lookupMsg, setLookupMsg] = useState<string | null>(null);
  const [prevTarget, setPrevTarget] = useState<{ ra_hours: number; dec_deg: number } | null>(null);
  const [recenter, setRecenter] = useState(0);
  const [focPos, setFocPos] = useState("12000");
  const [lon, setLon] = useState<number | null>(null);
  const [lat, setLat] = useState<number | null>(null);
  const [now, setNow] = useState(() => new Date());
  const [activity, setActivity] = useState<ActivityEvent[]>([]);

  const [imgT, setImgT] = useState(0);
  const [guideImgT, setGuideImgT] = useState(0);
  const lastImg = useRef<string | null>(null);
  const lastGuide = useRef<string | null>(null);

  const call = useCallback(async (fn: () => Promise<unknown>, after?: () => void) => {
    setErr(null);
    try {
      await fn();
      after?.();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  }, []);

  useEffect(() => {
    getJSON<{ location?: { longitude_deg?: number; latitude_deg?: number } }>("/api/observatory")
      .then((o) => {
        setLon(o.location?.longitude_deg ?? null);
        setLat(o.location?.latitude_deg ?? null);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const refreshActivity = useCallback(() => {
    getJSON<ActivityEvent[]>("/api/activity?limit=80").then(setActivity).catch(() => {});
  }, []);
  useEffect(() => {
    refreshActivity();
    const t = setInterval(refreshActivity, 3000);
    return () => clearInterval(t);
  }, [refreshActivity]);

  useEffect(() => {
    if (tel?.last_image_at && tel.last_image_at !== lastImg.current) {
      lastImg.current = tel.last_image_at;
      setImgT(Date.now());
    }
    if (tel?.last_guide_image_at && tel.last_guide_image_at !== lastGuide.current) {
      lastGuide.current = tel.last_guide_image_at;
      setGuideImgT(Date.now());
    }
  }, [tel?.last_image_at, tel?.last_guide_image_at]);

  const lookup = async () => {
    if (!target.trim()) return;
    setResolving(true);
    setErr(null);
    setLookupMsg("resolving…");
    try {
      const r = await getJSON<ResolveResult>(`/api/resolve?name=${encodeURIComponent(target.trim())}`);
      // keep the current target as the previous (dimmed) circle before moving
      const pr = parseFloat(ra);
      const pd = parseFloat(dec);
      setPrevTarget(!isNaN(pr) && !isNaN(pd) ? { ra_hours: pr, dec_deg: pd } : null);
      setRa(r.ra_hours.toFixed(4));
      setDec(r.dec_deg.toFixed(4));
      setRecenter((n) => n + 1); // center the map on it
      setLookupMsg(
        `✓ ${r.name} → RA ${r.ra_hours.toFixed(4)} h · Dec ${r.dec_deg >= 0 ? "+" : ""}${r.dec_deg.toFixed(4)}°`
      );
    } catch (e) {
      setLookupMsg(`✗ ${String(e instanceof Error ? e.message : e)}`);
    } finally {
      setResolving(false);
    }
  };

  const m = tel?.mount;
  const c = tel?.camera;
  const f = tel?.focuser;
  const prec = tel?.precision;
  const af = prec?.autofocus ?? {
    running: false, ok: null, best_position: null, best_hfr: null, samples: [], message: "idle",
  };
  const w = tel?.filter;
  const g = tel?.guider;
  const alt = m?.alt_deg ?? null;
  const airmass = alt && alt > 3 ? 1 / Math.sin((alt * Math.PI) / 180) : null;
  const lst = lon != null ? lstHours(now, lon) : null;
  const coordsValid =
    ra.trim() !== "" && dec.trim() !== "" && !isNaN(parseFloat(ra)) && !isNaN(parseFloat(dec));
  const targetRa = coordsValid ? parseFloat(ra) : null;
  const targetDec = coordsValid ? parseFloat(dec) : null;

  return (
    <div className="livectl">
      <SafetyBanner tel={tel} />
      <ConfirmBanner tel={tel} />

      {/* status strip */}
      <section className="card statusstrip">
        <div className="clock"><span className="muted">LOCAL</span><b>{now.toLocaleTimeString("en-GB")}</b></div>
        <div className="clock"><span className="muted">UTC</span><b>{now.toLocaleTimeString("en-GB", { timeZone: "UTC" })}</b></div>
        <div className="clock"><span className="muted">LST</span><b>{hms(lst)}</b></div>
        <div className="clock"><span className="muted">DATE</span><b>{now.toLocaleDateString("en-CA", { timeZone: "UTC" })}</b></div>
        <span className={`pill ${tel?.indi_connected ? "ok" : "bad"}`}>
          INDI {tel?.indi_connected ? "connected" : "down"}
        </span>
        {tel?.executor && tel.executor.state !== "idle" && (
          <span className="pill warn">observing: {tel.executor.object}</span>
        )}
        <Almanac />
      </section>

      {err && <div className="err">{err}</div>}

      <div className="livegrid">
        {/* left column: mount, lookup, filter+focus */}
        <div className="leftcol">
          <section className="card">
            <h2>Mount</h2>
            <div className="statgrid">
              <span>RA (h)</span><b>{fmt(m?.ra_hours, 4)}</b>
              <span>Dec (°)</span><b>{fmt(m?.dec_deg, 4)}</b>
              <span>Alt (°)</span><b>{fmt(alt, 1)}</b>
              <span>Az (°)</span><b>{fmt(m?.az_deg, 1)}</b>
              <span>Airmass</span><b>{fmt(airmass, 2)}</b>
              <span>State</span>
              <b>{m?.slewing ? "slewing" : m?.tracking ? "tracking" : "idle"}{m?.parked ? " · parked" : ""}</b>
            </div>
            <div className="row">
              <button className={m?.tracking ? "active" : ""} onClick={() => call(() => post("/api/mount/track", { on: !m?.tracking }))}>{m?.tracking ? "Tracking" : "Track on"}</button>
              <button className="danger" onClick={() => call(() => post("/api/mount/abort"))}>Abort</button>
              <button onClick={() => call(() => post(m?.parked ? "/api/mount/unpark" : "/api/mount/park"))}>{m?.parked ? "Unpark" : "Park"}</button>
              <button onClick={() => call(() => post("/api/mount/home"))}>Home</button>
            </div>
          </section>

          <section className="card">
            <h2>Lookup &amp; Target</h2>
            <div className="row">
              <label style={{ flex: 1 }}>
                Object name
                <input value={target} onChange={(e) => setTarget(e.target.value)}
                       onKeyDown={(e) => e.key === "Enter" && lookup()}
                       placeholder="M51, NGC 7000, SN 2024abc…" />
              </label>
              <button onClick={lookup} disabled={resolving}>{resolving ? "…" : "Lookup"}</button>
            </div>
            {lookupMsg && <div className="muted" style={{ margin: "8px 0" }}>{lookupMsg}</div>}
            {/* resolved (or hand-entered) target coordinates — editable */}
            <div className="row">
              <label>RA (h)<input value={ra} placeholder="—" onChange={(e) => setRa(e.target.value)} /></label>
              <label>Dec (°)<input value={dec} placeholder="—" onChange={(e) => setDec(e.target.value)} /></label>
              <button className="small" disabled={!coordsValid} onClick={() => setRecenter((n) => n + 1)} title="Center map on these coordinates">Center</button>
            </div>
            {coordsValid && <VisibilityAlert raHours={parseFloat(ra)} decDeg={parseFloat(dec)} />}
            <div className="row">
              <button className="active" disabled={!coordsValid} onClick={() => call(() => post("/api/mount/slew", { ra_hours: parseFloat(ra), dec_deg: parseFloat(dec), track: true }))}>Slew to target</button>
              <button disabled={!coordsValid} onClick={() => call(() => post("/api/mount/sync", { ra_hours: parseFloat(ra), dec_deg: parseFloat(dec) }))}>Sync</button>
              <button disabled={!coordsValid || prec?.busy || prec?.enabled === false}
                      title={prec?.enabled === false ? "solver disabled (CRITO_SOLVER)" : "plate-solve & center on target"}
                      onClick={() => call(() => post("/api/center", { ra_hours: parseFloat(ra), dec_deg: parseFloat(dec) }))}>
                {prec?.center?.running ? "Solving…" : "Solve & center"}
              </button>
            </div>
            {prec?.center && (prec.center.running || prec.center.message !== "idle") && (
              <div className={prec.center.ok === false ? "err" : "muted"} style={{ marginTop: 8 }}>
                plate-solve: {prec.center.message}
                {prec.center.error_arcsec != null && !prec.center.running ? ` · err ${prec.center.error_arcsec}″` : ""}
              </div>
            )}
          </section>

          <section className="card">
            <h2>Filter &amp; Focuser</h2>
            <div className="kv"><span>Filter</span><b>{w?.name ?? "—"}{w?.moving ? " ⟳" : ""}</b></div>
            <div className="row">
              {(w?.names ?? []).map((name, i) => (
                <button key={name} disabled={!w?.connected}
                        className={w?.position === i + 1 ? "active" : ""}
                        onClick={() => call(() => post("/api/filter/set", { slot: i + 1 }))}>
                  {name}
                </button>
              ))}
              {!w?.names?.length && <span className="muted">no filter wheel</span>}
            </div>
            <div className="kv"><span>Focus</span><b>{fmt(f?.position, 0)}{f?.moving ? " ⟳" : ""}</b></div>
            <div className="row">
              <label>Position<input value={focPos} onChange={(e) => setFocPos(e.target.value)} /></label>
              <button disabled={!f?.connected} onClick={() => call(() => post("/api/focuser/move", { position: parseFloat(focPos) }))}>Go</button>
              <button disabled={!f?.connected} onClick={() => call(() => post("/api/focuser/rel", { steps: 100, inward: true }))}>−100</button>
              <button disabled={!f?.connected} onClick={() => call(() => post("/api/focuser/rel", { steps: 100, inward: false }))}>+100</button>
              <button className={prec?.autofocus?.running ? "active" : ""}
                      disabled={!f?.connected || prec?.busy || prec?.enabled === false}
                      title={prec?.enabled === false ? "solver disabled (CRITO_SOLVER)" : "HFR V-curve autofocus"}
                      onClick={() => call(() => post("/api/focuser/autofocus"))}>
                {prec?.autofocus?.running ? "Focusing…" : "Autofocus"}
              </button>
            </div>
            <div className={af.ok === false ? "err" : "muted"} style={{ marginTop: 8 }}>
              autofocus: {af.message}
              {af.best_position != null && !af.running ? ` · best @ ${af.best_position} (HFR ${af.best_hfr ?? "—"})` : ""}
            </div>
            <AutofocusPlot af={af} />
          </section>
        </div>

        {/* right column: sky map */}
        <div className="skycol">
          <section className="card skycard">
            <h2>Sky <span className="muted">· green box = scope · red circle = target</span></h2>
            <SkyMap
              scopeRaHours={m?.ra_hours ?? null}
              scopeDecDeg={m?.dec_deg ?? null}
              targetRaHours={targetRa}
              targetDecDeg={targetDec}
              prevRaHours={prevTarget?.ra_hours ?? null}
              prevDecDeg={prevTarget?.dec_deg ?? null}
              recenter={recenter}
              lat={lat}
              lon={lon}
            />
          </section>
        </div>
      </div>

      {/* cameras side by side */}
      <div className="camrow">
        <CameraCard title="Imaging Camera" tel={c ?? null} apiBase="/api/camera"
                    imgT={imgT} call={call} refreshArchive={onCapture ?? (() => {})}
                    filterSlot={w?.position ?? undefined} objectName={target} />
        <CameraCard title="Guide Camera" tel={g ?? null} apiBase="/api/guide"
                    imgT={guideImgT} call={call} refreshArchive={() => {}} />
      </div>

      {/* auto guider — live PHD2 guiding plot (RA + Dec error) */}
      <section className="card">
        <h2>Auto Guider</h2>
        <GuidingPlot />
        <div className="row">
          <button disabled={!tel?.guiding?.connected}
                  onClick={() => call(() => post("/api/guiding/start"))}>Start guiding</button>
          <button disabled={!tel?.guiding?.connected}
                  onClick={() => call(() => post("/api/guiding/stop"))}>Stop</button>
          <span className="muted">
            {tel?.guiding?.connected
              ? `PHD2 ${tel.guiding.state}`
              : "PHD2 not connected — start PHD2 + enable its server (RUNBOOK §10)"}
          </span>
        </div>
      </section>

      {/* activity console */}
      <section className="card console">
        <h2>Activity <button className="small" onClick={refreshActivity}>refresh</button></h2>
        <div className="loglines">
          {!activity.length && <div className="muted">no activity yet</div>}
          {activity.map((e, i) => (
            <div key={i} className={`logline ${KIND_CLASS[e.kind] ?? "k-info"}`}>
              <span className="logts">{e.ts.slice(11, 19)}</span> {e.msg}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
