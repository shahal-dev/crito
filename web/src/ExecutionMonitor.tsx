import { useCallback, useEffect, useState } from "react";
import { Plan, QueueBlock, Telemetry, getJSON, post } from "./api";
import ConfirmBanner from "./ConfirmBanner";

function fmtTime(iso: string | null | undefined, tz: string): string {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit" });
}

export default function ExecutionMonitor({ tel }: { tel: Telemetry | null }) {
  const [queue, setQueue] = useState<QueueBlock[]>([]);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [q, p] = await Promise.all([
        getJSON<QueueBlock[]>("/api/transient/queue"),
        getJSON<Plan[]>("/api/transient/plans"),
      ]);
      setQueue(q);
      setPlans(p);
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  const call = async (fn: () => Promise<unknown>) => {
    setErr(null);
    setBusy(true);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  const move = (idx: number, dir: -1 | 1) => {
    const ids = queue.map((b) => b.id);
    const j = idx + dir;
    if (j < 0 || j >= ids.length) return;
    [ids[idx], ids[j]] = [ids[j], ids[idx]];
    call(() => post("/api/transient/queue/reorder", { block_ids: ids }));
  };

  const ex = tel?.executor ?? null;
  const running = ex && ex.state !== "idle";
  const pct = ex && ex.total ? Math.round((ex.current_step / ex.total) * 100) : 0;

  return (
    <div>
      <ConfirmBanner tel={tel} />
      <section className="card">
        <h2>
          Now observing
          {ex?.manual_override && <span className="pill warn" style={{ marginLeft: 8 }}>manual override</span>}
          <span className={`pill ${ex?.auto_execute ? "ok" : "idle"}`} style={{ marginLeft: 8 }}>
            auto-exec {ex?.auto_execute ? "on" : "off"}
          </span>
        </h2>
        {running ? (
          <>
            <div className="kv">
              <span>Target</span>
              <b>{ex!.object ?? "—"} <span className="muted">({ex!.mode})</span></b>
            </div>
            <div className="kv">
              <span>Step</span>
              <b>{ex!.step ?? "—"} <span className="muted">· {ex!.current_step}/{ex!.total}</span></b>
            </div>
            <div className="kv">
              <span>Exposure left</span>
              <b>{ex!.exposure_remaining > 0 ? `${ex!.exposure_remaining.toFixed(0)} s` : "—"}</b>
            </div>
            <div className="kv">
              <span>Frames</span>
              <b>{ex!.n_done} done{ex!.n_failed ? `, ${ex!.n_failed} failed` : ""}</b>
            </div>
            <div className="progress">
              <div className="bar" style={{ width: `${pct}%` }} />
            </div>
            <div className="row">
              {ex!.state === "paused" ? (
                <button onClick={() => call(() => post("/api/transient/executor/resume"))} disabled={busy}>
                  Resume
                </button>
              ) : (
                <button onClick={() => call(() => post("/api/transient/executor/pause"))} disabled={busy}>
                  Pause
                </button>
              )}
              <button className="danger" onClick={() => call(() => post("/api/transient/executor/abort"))} disabled={busy}>
                Abort
              </button>
            </div>
          </>
        ) : (
          <div className="muted">
            idle — no block running.{" "}
            {ex?.manual_override && "Manual override is on; "}
            launch a queued block below.
            {ex?.manual_override && (
              <button
                className="small"
                onClick={() => call(() => post("/api/transient/executor/override", { on: false }))}
              >
                clear override
              </button>
            )}
          </div>
        )}
      </section>

      {err && <div className="err">{err}</div>}

      <section className="card">
        <h2>Plans <span className="muted">· {plans.length}</span></h2>
        {!plans.length && <div className="muted">no saved plans — build one in the Plan tab.</div>}
        {plans.map((p) => (
          <div className="candrow" key={p.id}>
            <div className="candhead">
              <b style={{ color: "#fff" }}>{p.name}</b>
              {p.object_name && <span className="pill idle">{p.object_name}</span>}
              <span className="muted">
                {(p.recipe_json ?? []).reduce((a, r) => a + (r.count || 0), 0) * p.repeat} shots
              </span>
              {p.last_block_id && <span className="pill ok">has run</span>}
            </div>
            <div className="row">
              <button className="active" disabled={busy}
                      onClick={() => call(() => post(`/api/transient/plans/${p.id}/run?resume=false`))}>Run</button>
              <button disabled={busy || !p.last_block_id}
                      title={p.last_block_id ? "Continue, skipping completed shots" : "Run once first"}
                      onClick={() => call(() => post(`/api/transient/plans/${p.id}/run?resume=true`))}>Resume</button>
            </div>
          </div>
        ))}
      </section>

      <section className="card">
        <h2>
          Queue <span className="muted">· {queue.length}</span>
          <button className="small" onClick={refresh}>refresh</button>
        </h2>
        {!queue.length && <div className="muted">queue is empty — approve a candidate to Queue or Execute.</div>}
        {queue.map((b, i) => (
          <div className="candrow" key={b.id}>
            <div className="candhead">
              <b style={{ color: "#fff" }}>{b.request?.object_name ?? b.id}</b>
              {b.class_label && <span className="pill idle">{b.class_label}</span>}
              <span className="pill idle">{b.request?.mode}</span>
              <span className={`pill ${b.state === "running" ? "ok" : b.state === "paused" ? "warn" : "idle"}`}>
                {b.state}
              </span>
              <span className="muted">
                {b.n_done}/{b.total_steps - 3} frames · window{" "}
                {fmtTime(b.request?.window_start_utc, "Asia/Dhaka")}–{fmtTime(b.request?.window_end_utc, "Asia/Dhaka")} BST
              </span>
            </div>
            <div className="row">
              <button
                className="active"
                disabled={busy || b.state !== "queued"}
                onClick={() => call(() => post("/api/transient/executor/launch", { block_id: b.id }))}
              >
                Launch
              </button>
              <button className="small" disabled={busy || i === 0} onClick={() => move(i, -1)}>↑</button>
              <button className="small" disabled={busy || i === queue.length - 1} onClick={() => move(i, 1)}>↓</button>
            </div>
          </div>
        ))}
      </section>
    </div>
  );
}
