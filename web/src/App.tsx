import { useCallback, useEffect, useState } from "react";
import CritoLogo from "./CritoLogo";
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import {
  DashTelescope, ImageRec, SiteRef, Telemetry,
  atLeast, clearAuth, getJSON, getRole, getUsername, isAuthed, mediaUrl, post, setApiBase, wsUrl,
} from "./api";
import BottomTerminal from "./BottomTerminal";
import Candidates from "./Candidates";
import Dashboard from "./Dashboard";
import Devices from "./Devices";
import ExecutionMonitor from "./ExecutionMonitor";
import ExposurePlanner from "./ExposurePlanner";
import LiveControl from "./LiveControl";
import Login from "./Login";
import PlanPage from "./PlanPage";
import Users from "./Users";

type Active = { site: SiteRef; telescope: DashTelescope };

function loadActive(): Active | null {
  try {
    const s = localStorage.getItem("crito_active");
    if (s) {
      const a = JSON.parse(s) as Active;
      setApiBase(a.site.url);
      return a;
    }
  } catch {
    /* ignore */
  }
  return null;
}

export default function App() {
  const navigate = useNavigate();
  const onDashboard = useLocation().pathname === "/";
  const [authed, setAuthed] = useState(isAuthed());
  const [tel, setTel] = useState<Telemetry | null>(null);
  const [wsOk, setWsOk] = useState(false);
  const [images, setImages] = useState<ImageRec[]>([]);
  const [active, setActiveState] = useState<Active | null>(loadActive);

  const setActive = (a: Active | null) => {
    setActiveState(a);
    if (a) localStorage.setItem("crito_active", JSON.stringify(a));
    else localStorage.removeItem("crito_active");
  };

  // validate a stored token on load
  useEffect(() => {
    if (!isAuthed()) return;
    getJSON("/api/auth/me").catch(() => {
      clearAuth();
      setAuthed(false);
    });
  }, []);

  // restore: reconnect the backend to the persisted telescope on first load
  useEffect(() => {
    if (active) {
      post("/api/indi/server", { host: active.telescope.indi_host, port: active.telescope.indi_port }).catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
    navigate("/console");
  };

  const logout = () => {
    clearAuth();
    setActive(null);
    setAuthed(false);
    navigate("/");
  };

  // operational pages require a selected telescope
  const requireScope = (el: JSX.Element) => (active ? el : <Navigate to="/" replace />);

  const consolePage = (
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
  );

  return (
    <div className="app">
      <header>
        <CritoLogo onClick={() => navigate("/")} />
        {!onDashboard && active && (
          <span className="muted" style={{ fontSize: 12 }}>
            {active.site.name} · {active.telescope.name}
          </span>
        )}
        {!onDashboard && active && (
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
        {!onDashboard && (
          <nav className="tabs">
            <NavLink to="/" end>Dashboard</NavLink>
            {active && <NavLink to="/console">Console</NavLink>}
            {active && <NavLink to="/candidates">Candidates</NavLink>}
            {active && <NavLink to="/plan">Plan</NavLink>}
            {active && <NavLink to="/exposure">Exposure</NavLink>}
            {active && <NavLink to="/observe">Observe</NavLink>}
            {atLeast("admin") && <NavLink to="/users">Users</NavLink>}
          </nav>
        )}
        <span className="muted" style={{ fontSize: 12, marginLeft: onDashboard ? "auto" : 10 }}>{getUsername()} · {getRole()}</span>
        <button className="small" onClick={logout}>Logout</button>
      </header>

      <Routes>
        <Route path="/" element={<Dashboard onOperate={onOperate} />} />
        <Route path="/console" element={requireScope(consolePage)} />
        <Route path="/candidates" element={requireScope(<Candidates />)} />
        <Route path="/plan" element={requireScope(<PlanPage tel={tel} />)} />
        <Route path="/exposure" element={requireScope(<ExposurePlanner />)} />
        <Route path="/observe" element={requireScope(<ExecutionMonitor tel={tel} />)} />
        <Route path="/users" element={atLeast("admin") ? <Users /> : <Navigate to="/" replace />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>

      <footer>{active ? `last update ${tel?.ts ?? "—"}` : "CRITO · select a location"}</footer>

      {active && <BottomTerminal tel={tel} />}
    </div>
  );
}
