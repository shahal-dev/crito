import { useCallback, useEffect, useRef, useState } from "react";
import { getJSON, ImageRec, post, Telemetry } from "./api";
import Devices from "./Devices";

function fmt(n: number | null | undefined, d = 4): string {
  return n === null || n === undefined ? "—" : n.toFixed(d);
}

export default function App() {
  const [tel, setTel] = useState<Telemetry | null>(null);
  const [wsOk, setWsOk] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // mount form
  const [ra, setRa] = useState("5.59");
  const [dec, setDec] = useState("-5.39");
  const [track, setTrack] = useState(true);

  // capture form
  const [obj, setObj] = useState("M42");
  const [exp, setExp] = useState("2");
  const [imgType, setImgType] = useState("LIGHT");

  // focuser
  const [focPos, setFocPos] = useState("12000");

  const [imgT, setImgT] = useState(0);
  const [images, setImages] = useState<ImageRec[]>([]);
  const lastImg = useRef<string | null>(null);

  const refreshArchive = useCallback(async () => {
    try {
      setImages(await getJSON<ImageRec[]>("/api/images?limit=24"));
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let stop = false;
    const connect = () => {
      ws = new WebSocket(`ws://${location.host}/ws/telemetry`);
      ws.onopen = () => setWsOk(true);
      ws.onclose = () => {
        setWsOk(false);
        if (!stop) setTimeout(connect, 1500);
      };
      ws.onmessage = (e) => {
        const t: Telemetry = JSON.parse(e.data);
        setTel(t);
        if (t.last_image_at && t.last_image_at !== lastImg.current) {
          lastImg.current = t.last_image_at;
          setImgT(Date.now());
        }
      };
    };
    connect();
    refreshArchive();
    return () => {
      stop = true;
      if (ws) {
        ws.onclose = null; // don't trigger a reconnect from the torn-down socket
        ws.onerror = null;
        ws.close();
      }
    };
  }, [refreshArchive]);

  const call = async (fn: () => Promise<unknown>, after?: () => void) => {
    setErr(null);
    try {
      await fn();
      after?.();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  };

  const m = tel?.mount;
  const c = tel?.camera;
  const f = tel?.focuser;
  const w = tel?.filter;

  return (
    <div className="app">
      <header>
        <h1>CASSA · Virtual Site</h1>
        <span className={`pill ${wsOk ? "ok" : "bad"}`}>{wsOk ? "live" : "offline"}</span>
        <span className={`pill ${tel?.indi_connected ? "ok" : "bad"}`}>
          INDI {tel?.indi_connected ? "connected" : "down"}
        </span>
      </header>

      {err && <div className="err">{err}</div>}

      <Devices tel={tel} />

      <div className="grid">
        {/* Mount */}
        <section className="card">
          <h2>Mount</h2>
          <div className="kv"><span>RA (h)</span><b>{fmt(m?.ra_hours)}</b></div>
          <div className="kv"><span>Dec (°)</span><b>{fmt(m?.dec_deg)}</b></div>
          <div className="kv"><span>Alt / Az (°)</span><b>{fmt(m?.alt_deg, 1)} / {fmt(m?.az_deg, 1)}</b></div>
          <div className="badges">
            <span className={`pill ${m?.slewing ? "warn" : "idle"}`}>{m?.slewing ? "slewing" : "idle"}</span>
            <span className={`pill ${m?.tracking ? "ok" : "idle"}`}>{m?.tracking ? "tracking" : "no track"}</span>
            <span className={`pill ${m?.parked ? "warn" : "idle"}`}>{m?.parked ? "parked" : "unparked"}</span>
          </div>
          <div className="row">
            <label>RA (h)<input value={ra} onChange={(e) => setRa(e.target.value)} /></label>
            <label>Dec (°)<input value={dec} onChange={(e) => setDec(e.target.value)} /></label>
            <label className="chk"><input type="checkbox" checked={track} onChange={(e) => setTrack(e.target.checked)} /> track</label>
          </div>
          <div className="row">
            <button onClick={() => call(() => post("/api/mount/slew", { ra_hours: parseFloat(ra), dec_deg: parseFloat(dec), track }))}>Slew</button>
            <button className="danger" onClick={() => call(() => post("/api/mount/abort"))}>Abort</button>
            <button onClick={() => call(() => post("/api/mount/park"))}>Park</button>
            <button onClick={() => call(() => post("/api/mount/unpark"))}>Unpark</button>
          </div>
        </section>

        {/* Focuser + Filter */}
        <section className="card">
          <h2>Focuser &amp; Filter</h2>
          <div className="kv"><span>Focus position</span><b>{fmt(f?.position, 0)}{f?.moving ? " ⟳" : ""}</b></div>
          <div className="row">
            <label>Position<input value={focPos} onChange={(e) => setFocPos(e.target.value)} /></label>
            <button disabled={!f?.connected} onClick={() => call(() => post("/api/focuser/move", { position: parseFloat(focPos) }))}>Go</button>
            <button disabled={!f?.connected} onClick={() => call(() => post("/api/focuser/rel", { steps: 100, inward: true }))}>−100</button>
            <button disabled={!f?.connected} onClick={() => call(() => post("/api/focuser/rel", { steps: 100, inward: false }))}>+100</button>
          </div>
          <div className="kv"><span>Filter</span><b>{w?.name ?? "—"}{w?.moving ? " ⟳" : ""}</b></div>
          <div className="row">
            {(w?.names ?? []).map((name, i) => (
              <button
                key={name}
                disabled={!w?.connected}
                className={w?.position === i + 1 ? "active" : ""}
                onClick={() => call(() => post("/api/filter/set", { slot: i + 1 }))}
              >
                {name}
              </button>
            ))}
            {!w?.names?.length && <span className="muted">no filter wheel</span>}
          </div>
        </section>

        {/* Camera + capture */}
        <section className="card">
          <h2>Camera</h2>
          <div className="badges">
            <span className={`pill ${c?.exposing ? "warn" : "idle"}`}>{c?.exposing ? "exposing" : "idle"}</span>
            <span className="pill idle">t− {fmt(c?.exposure_remaining, 1)} s</span>
          </div>
          <div className="row">
            <label>Object<input value={obj} onChange={(e) => setObj(e.target.value)} /></label>
            <label>Exp (s)<input value={exp} onChange={(e) => setExp(e.target.value)} /></label>
            <label>Type
              <select value={imgType} onChange={(e) => setImgType(e.target.value)}>
                <option>LIGHT</option><option>DARK</option><option>BIAS</option><option>FLAT</option>
              </select>
            </label>
          </div>
          <div className="row">
            <button onClick={() => call(
              () => post("/api/camera/capture", {
                seconds: parseFloat(exp), image_type: imgType, object_name: obj,
                filter_slot: w?.position ?? undefined,
              }),
              refreshArchive,
            )}>Capture &amp; archive</button>
            <button onClick={() => call(() => post("/api/camera/expose", { seconds: parseFloat(exp) }))}>Quick expose</button>
          </div>
          <div className="preview">
            {imgT ? (
              <img src={`/api/camera/last-image.png?t=${imgT}`} alt="last frame" />
            ) : (
              <div className="noimg">no image yet — capture a frame</div>
            )}
          </div>
        </section>
      </div>

      {/* Archive */}
      <section className="card archive">
        <h2>Archive <button className="small" onClick={refreshArchive}>refresh</button></h2>
        {!images.length && <div className="muted">no images archived yet</div>}
        <div className="thumbs">
          {images.map((im) => (
            <div className="thumb" key={im.id} title={im.obsid}>
              <img src={`/api/images/${im.id}/thumb.png`} alt={im.obsid} />
              <div className="meta">
                <b>{im.object_name || im.image_type}</b>
                <span>{im.image_type} · {im.filter ?? "—"} · {im.exptime}s</span>
                <span className="muted">{im.date_obs.replace("T", " ").slice(0, 19)}</span>
                <a href={`/api/images/${im.id}/fits`}>FITS ↓</a>
              </div>
            </div>
          ))}
        </div>
      </section>

      <footer>last update {tel?.ts ?? "—"}</footer>
    </div>
  );
}
