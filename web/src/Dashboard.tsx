import { useCallback, useEffect, useState } from "react";
import { DashTelescope, SiteInfo, SiteRef, siteGet } from "./api";

const STATUS_PILL: Record<string, string> = {
  online: "ok",
  standby: "warn",
  maintenance: "warn",
  offline: "bad",
};

type SiteState = { ref: SiteRef; info?: SiteInfo; err?: string };

function weatherIcon(cond?: string | null): string {
  const c = (cond || "").toLowerCase();
  if (c.includes("rain")) return "🌧️";
  if (c.includes("cloud")) return "☁️";
  if (c.includes("clear") || c.includes("sunny")) return "🌙";
  return "🔭";
}

export default function Dashboard({
  onOperate,
}: {
  onOperate: (site: SiteRef, t: DashTelescope) => void;
}) {
  const [sites, setSites] = useState<SiteState[]>([]);

  const load = useCallback(() => {
    fetch("/sites.json")
      .then((r) => r.json())
      .then((refs: SiteRef[]) => {
        setSites((prev) =>
          refs.map((r) => prev.find((p) => p.ref.id === r.id) ?? { ref: r })
        );
        refs.forEach((r) => {
          siteGet<SiteInfo>(r.url, "/api/site")
            .then((info) =>
              setSites((s) => s.map((e) => (e.ref.id === r.id ? { ref: e.ref, info } : e)))
            )
            .catch((err) =>
              setSites((s) => s.map((e) => (e.ref.id === r.id ? { ref: e.ref, err: String(err) } : e)))
            );
        });
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <div className="dashboard">
      <h1 className="dash-title">Telescope Locations</h1>
      <div className="sitegrid">
        {sites.map(({ ref, info, err }) => (
          <section className="card sitecard" key={ref.id}>
            <div className="candhead">
              <b style={{ color: "#fff", fontSize: 15 }}>{info?.name ?? ref.name}</b>
              <span className={`pill ${STATUS_PILL[info?.status ?? (err ? "offline" : "standby")] ?? "idle"}`}>
                {err ? "unreachable" : info?.status ?? "…"}
              </span>
              {info?.safety && (
                <span className={`pill ${info.safety === "safe" ? "ok" : info.safety === "warn" ? "warn" : "bad"}`}
                      title="safety state">
                  {info.safety}
                </span>
              )}
            </div>

            {err && <div className="muted" style={{ marginTop: 8 }}>can't reach {ref.url || "this site"} — {err}</div>}

            {info?.weather && (
              <div className="weatherrow">
                <span>{weatherIcon(info.weather.condition)} {info.weather.condition ?? "—"}</span>
                {info.weather.seeing && <span className="muted">seeing {info.weather.seeing}</span>}
                {info.weather.humidity != null && <span className="muted">humidity {info.weather.humidity}%</span>}
              </div>
            )}

            <div className="muted" style={{ margin: "10px 0 4px", fontSize: 11, letterSpacing: 1 }}>TELESCOPES</div>
            {info && !info.telescopes.length && <div className="muted">no telescopes configured</div>}
            {info?.telescopes.map((t) => (
              <div className="telrow" key={t.id}>
                <span className="telname">{t.name}</span>
                <span className={`pill ${STATUS_PILL[t.status] ?? "idle"}`}>{t.status}</span>
                <button className="small active" onClick={() => onOperate(ref, t)}>Operate</button>
              </div>
            ))}
            {!info && !err && <div className="muted">loading…</div>}
          </section>
        ))}
        {!sites.length && <div className="muted">no sites in sites.json</div>}
      </div>
    </div>
  );
}
