import { Telemetry, post } from "./api";

const STATE: Record<string, { cls: string; label: string }> = {
  safe: { cls: "ok", label: "SAFE" },
  warn: { cls: "warn", label: "WARN" },
  unsafe: { cls: "bad", label: "UNSAFE" },
  fault: { cls: "bad", label: "FAULT" },
};

export default function SafetyBanner({ tel }: { tel: Telemetry | null }) {
  const sf = tel?.safety;
  if (!sf) return null;
  const st = STATE[sf.state] ?? { cls: "idle", label: sf.state };
  const w = sf.weather || {};
  const call = (fn: () => Promise<unknown>) => fn().catch(() => {});

  return (
    <section className={`card safetybar safety-${sf.state}`}>
      <div className="safetyhead">
        <span className={`pill ${st.cls}`} style={{ fontSize: 13 }}>● {st.label}</span>
        {!sf.enabled && <span className="muted">safety disabled</span>}
        {sf.override && <span className="pill warn">OVERRIDE ON</span>}
        <span className="muted">{sf.reasons?.length ? sf.reasons.join(" · ") : "conditions ok"}</span>
        <span className="muted" style={{ marginLeft: "auto" }}>
          {w.temperature != null && `${w.temperature}°C `}
          {w.humidity != null && `· RH ${w.humidity}% `}
          {w.clouds != null && `· clouds ${w.clouds}% `}
          {w.wind_speed != null && `· wind ${w.wind_speed} km/h `}
          {w.rain ? "· RAIN " : ""}
          {sf.sun_alt != null && `· sun ${sf.sun_alt.toFixed(0)}° `}
          {w.source && `(${w.source})`}
        </span>
      </div>
      <div className="row">
        {sf.estop ? (
          <button className="active" onClick={() => call(() => post("/api/safety/clear"))}>Clear e-stop</button>
        ) : (
          <button className="danger" onClick={() => call(() => post("/api/safety/estop"))}>■ Emergency stop</button>
        )}
        <button className={sf.override ? "active" : ""} onClick={() => call(() => post("/api/safety/override", { on: !sf.override }))}>
          {sf.override ? "Disable override" : "Override safety"}
        </button>
      </div>
    </section>
  );
}
