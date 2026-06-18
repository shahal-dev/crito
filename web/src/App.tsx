import { useCallback, useEffect, useState } from "react";
import {
  DashTelescope, ImageRec, SiteRef, Telemetry,
  atLeast, clearAuth, getJSON, getRole, getUsername, isAuthed, mediaUrl, post, setApiBase, wsUrl,
} from "./api";
import Candidates from "./Candidates";
import Dashboard from "./Dashboard";
import Devices from "./Devices";
import ExecutionMonitor from "./ExecutionMonitor";
import LiveControl from "./LiveControl";
import Login from "./Login";
import PlanPage from "./PlanPage";
import Users from "./Users";

type Tab = "dashboard" | "console" | "candidates" | "plan" | "observing" | "users";

export default function App() {
  const [authed, setAuthed] = useState(isAuthed());
  const [tel, setTel] = useState<Telemetry | null>(null);
  const [wsOk, setWsOk] = useState(false);
  const [tab, setTab] = useState<Tab>("dashboard");
  const [images, setImages] = useState<ImageRec[]>([]);
  const [active, setActive] = useState<{ site: SiteRef; telescope: DashTelescope } | null>(null);

  // validate a stored token on load
  useEffect(() => {
    if (!isAuthed()) return;
    getJSON("/api/auth/me").catch(() => {
      clearAuth();
      setAuthed(false);
    });
  }, []);

  const refreshArchive = useCallback(async () => {
    try {
      setImages(await getJSON<ImageRec[]>("/api/images?limit=24"));
    } catch {
      /* ignore */
    }
  }, []);

  // telemetry WS — connects to the active site's backend; reconnects when it changes
  useEffect(() => {
    if (!active || !authed) {
      setTel(null);
      setWsOk(false);
      return;
    }
    let ws: WebSocket | null = null;
    let stop = false;
    const connect = () => {
      ws = new WebSocket(wsUrl("/ws/telemetry"));
      ws.onopen = () => setWsOk(true);
      ws.onclose = () => {
        setWsOk(false);
        if (!stop) setTimeout(connect, 1500);
      };
      ws.onmessage = (e) => setTel(JSON.parse(e.data) as Telemetry);
    };
    connect();
    refreshArchive();
    return () => {
      stop = true;
      if (ws) {
        ws.onclose = null;
        ws.onerror = null;
        ws.close();
      }
    };
  }, [active, authed, refreshArchive]);

  if (!authed) return <Login onLogin={() => setAuthed(true)} />;

  const onOperate = (site: SiteRef, telescope: DashTelescope) => {
    setApiBase(site.url);
    post("/api/indi/server", { host: telescope.indi_host, port: telescope.indi_port }).catch(() => {});
    setActive({ site, telescope });
    setTab("console");
  };

  const logout = () => {
    clearAuth();
    setActive(null);
    setAuthed(false);
  };

  const needSite = !active && tab !== "dashboard" && tab !== "users";

  return (
    <div className="app">
      <header>
        <img src="/logo.png" className="logo" alt="CASSA" onClick={() => setTab("dashboard")} />
        {active && (
          <span className="muted" style={{ fontSize: 12 }}>
            {active.site.name} · {active.telescope.name}
          </span>
        )}
        {active && (
          <>
            <span className={`pill ${wsOk ? "ok" : "bad"}`}>{wsOk ? "live" : "offline"}</span>
            <span className={`pill ${tel?.indi_connected ? "ok" : "bad"}`}>
              INDI {tel?.indi_connected ? "connected" : "down"}
            </span>
            {tel?.safety && (
              <span
                className={`pill ${tel.safety.state === "safe" ? "ok" : tel.safety.state === "warn" ? "warn" : "bad"}`}
                title={tel.safety.reasons?.join(", ")}
              >
                {tel.safety.state}
              </span>
            )}
          </>
        )}
        <nav className="tabs">
          <button className={tab === "dashboard" ? "active" : ""} onClick={() => setTab("dashboard")}>Dashboard</button>
          <button className={tab === "console" ? "active" : ""} onClick={() => setTab("console")}>Console</button>
          <button className={tab === "candidates" ? "active" : ""} onClick={() => setTab("candidates")}>Candidates</button>
          <button className={tab === "plan" ? "active" : ""} onClick={() => setTab("plan")}>Plan</button>
          <button className={tab === "observing" ? "active" : ""} onClick={() => setTab("observing")}>Observe</button>
          {atLeast("admin") && (
            <button className={tab === "users" ? "active" : ""} onClick={() => setTab("users")}>Users</button>
          )}
        </nav>
        <span className="muted" style={{ fontSize: 12, marginLeft: 10 }}>{getUsername()} · {getRole()}</span>
        <button className="small" onClick={logout}>Logout</button>
      </header>

      {tab === "dashboard" && <Dashboard onOperate={onOperate} />}
      {tab === "users" && atLeast("admin") && <Users />}

      {needSite && (
        <div className="muted" style={{ padding: 24 }}>
          Select a telescope from the <b>Dashboard</b> to operate it.
        </div>
      )}

      {!needSite && tab === "candidates" && <Candidates />}
      {!needSite && tab === "plan" && <PlanPage tel={tel} />}
      {!needSite && tab === "observing" && <ExecutionMonitor tel={tel} />}

      {!needSite && tab === "console" && (
        <>
          <Devices tel={tel} />
          <LiveControl tel={tel} onCapture={refreshArchive} />

          <section className="card archive">
            <h2>
              Archive <button className="small" onClick={refreshArchive}>refresh</button>
            </h2>
            {!images.length && <div className="muted">no images archived yet</div>}
            <div className="thumbs">
              {images.map((im) => (
                <div className="thumb" key={im.id} title={im.obsid}>
                  <img src={mediaUrl(`/api/images/${im.id}/thumb.png`)} alt={im.obsid} />
                  <div className="meta">
                    <b>{im.object_name || im.image_type}</b>
                    <span>{im.image_type} · {im.filter ?? "—"} · {im.exptime}s</span>
                    <span className="muted">{im.date_obs.replace("T", " ").slice(0, 19)}</span>
                    <a href={mediaUrl(`/api/images/${im.id}/fits`)}>FITS ↓</a>
                  </div>
                </div>
              ))}
            </div>
          </section>
        </>
      )}

      <footer>{active ? `last update ${tel?.ts ?? "—"}` : "CASSA · select a location"}</footer>
    </div>
  );
}
