import { Telemetry, post } from "./api";

// Shown when the running sequence is paused waiting for the operator to confirm a
// prompt step (e.g. set up the flat-frame light source before flats are taken).
export default function ConfirmBanner({ tel }: { tel: Telemetry | null }) {
  const msg = tel?.executor?.awaiting_confirm;
  if (!msg) return null;
  return (
    <section className="card safetybar safety-warn">
      <div className="safetyhead">
        <span className="pill warn" style={{ fontSize: 13 }}>⏸ ACTION NEEDED</span>
        <span style={{ color: "#fff" }}>{msg}</span>
        <button className="active" style={{ marginLeft: "auto" }}
                onClick={() => post("/api/transient/executor/confirm").catch(() => {})}>
          Confirm &amp; continue
        </button>
      </div>
    </section>
  );
}
