import { useCallback, useEffect, useState } from "react";
import { ExposureSet, Plan, QueueBlock, ResolveResult, Telemetry, del, getJSON, post } from "./api";

const blankSet = (image_type = "LIGHT"): ExposureSet => ({
  filter_slot: null,
  filter_name: "",
  exptime_s: image_type === "BIAS" ? 0 : 60,
  count: image_type === "LIGHT" ? 5 : 15,
  binning: 1,
  dither_px: 0,
  image_type,
});

export default function PlanPage({ tel }: { tel: Telemetry | null }) {
  const [id, setId] = useState<string | null>(null);
  const [name, setName] = useState("New plan");
  const [objectName, setObjectName] = useState("");
  const [raHours, setRaHours] = useState("");
  const [decDeg, setDecDeg] = useState("");
  const [recipe, setRecipe] = useState<ExposureSet[]>([blankSet()]);
  const [repeat, setRepeat] = useState("1");
  const [autofocus, setAutofocus] = useState(false);
  const [center, setCenter] = useState(false);
  const [lastBlockId, setLastBlockId] = useState<string | null>(null);

  const [plans, setPlans] = useState<Plan[]>([]);
  const [queue, setQueue] = useState<QueueBlock[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [resolving, setResolving] = useState(false);

  const filterNames = tel?.filter?.names ?? [];

  const refresh = useCallback(() => {
    getJSON<Plan[]>("/api/transient/plans").then(setPlans).catch(() => {});
    getJSON<QueueBlock[]>("/api/transient/queue").then(setQueue).catch(() => {});
  }, []);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 8000);
    return () => clearInterval(t);
  }, [refresh]);

  const newPlan = () => {
    setId(null); setName("New plan"); setObjectName(""); setRaHours(""); setDecDeg("");
    setRecipe([blankSet()]); setRepeat("1"); setAutofocus(false); setCenter(false);
    setLastBlockId(null); setMsg(null); setErr(null);
  };

  const loadPlan = (p: Plan) => {
    setId(p.id); setName(p.name); setObjectName(p.object_name);
    setRaHours(p.ra_deg != null ? (p.ra_deg / 15).toFixed(4) : "");
    setDecDeg(p.dec_deg != null ? p.dec_deg.toFixed(4) : "");
    setRecipe(p.recipe_json && p.recipe_json.length ? p.recipe_json.map((e) => ({ ...e })) : [blankSet()]);
    setRepeat(String(p.repeat)); setAutofocus(p.autofocus); setCenter(p.center);
    setLastBlockId(p.last_block_id); setMsg(null); setErr(null);
  };

  const loadFromQueue = (b: QueueBlock) => {
    const r = b.request;
    if (!r) return;
    setObjectName(r.object_name);
    setRaHours((r.ra_deg / 15).toFixed(4));
    setDecDeg(r.dec_deg.toFixed(4));
    if (r.recipe_json && r.recipe_json.length) setRecipe(r.recipe_json.map((e) => ({ ...e })));
    setMsg(`loaded target ${r.object_name} from queue`);
  };

  const lookup = async () => {
    if (!objectName.trim()) return;
    setResolving(true);
    setErr(null);
    setMsg("resolving…");
    try {
      const r = await getJSON<ResolveResult>(`/api/resolve?name=${encodeURIComponent(objectName.trim())}`);
      setRaHours(r.ra_hours.toFixed(4));
      setDecDeg(r.dec_deg.toFixed(4));
      setMsg(`✓ ${r.name} → RA ${r.ra_hours.toFixed(4)} h · Dec ${r.dec_deg >= 0 ? "+" : ""}${r.dec_deg.toFixed(4)}°`);
    } catch (e) {
      setMsg(`✗ ${String(e instanceof Error ? e.message : e)}`);
    } finally {
      setResolving(false);
    }
  };

  const setRow = (i: number, patch: Partial<ExposureSet>) =>
    setRecipe((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  const payload = () => ({
    id: id ?? undefined,
    name: name.trim() || "Untitled plan",
    object_name: objectName.trim(),
    ra_deg: raHours.trim() === "" ? null : parseFloat(raHours) * 15,
    dec_deg: decDeg.trim() === "" ? null : parseFloat(decDeg),
    recipe,
    repeat: Math.max(1, parseInt(repeat) || 1),
    autofocus,
    center,
    source: id ? undefined : "manual",
  });

  const save = async (): Promise<Plan | null> => {
    setErr(null);
    try {
      const saved = (await post("/api/transient/plans", payload())) as Plan;
      setId(saved.id);
      setLastBlockId(saved.last_block_id);
      setMsg(`saved "${saved.name}"`);
      refresh();
      return saved;
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
      return null;
    }
  };

  const run = async (resume: boolean) => {
    setBusy(true);
    setErr(null);
    try {
      const saved = await save();
      if (!saved) return;
      const res = (await post(`/api/transient/plans/${saved.id}/run?resume=${resume}`)) as {
        block_id: string;
        resumed: boolean;
      };
      setLastBlockId(res.block_id);
      setMsg(res.resumed ? `resumed → block ${res.block_id.slice(0, 8)} (Observing tab)` : `launched → block ${res.block_id.slice(0, 8)} (Observing tab)`);
      refresh();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (pid: string) => {
    await del(`/api/transient/plans/${pid}`).catch(() => {});
    if (pid === id) newPlan();
    refresh();
  };

  const totalShots = recipe.reduce((a, r) => a + (Number(r.count) || 0), 0) * (parseInt(repeat) || 1);

  return (
    <div className="planpage">
      <div className="livegrid">
        {/* editor */}
        <div className="leftcol">
          <section className="card">
            <h2>Plan</h2>
            <div className="row">
              <label style={{ flex: 1 }}>Plan name<input value={name} onChange={(e) => setName(e.target.value)} /></label>
              <button className="small" onClick={newPlan}>New</button>
            </div>
            <div className="row">
              <label style={{ flex: 1 }}>
                Object
                <input value={objectName} onChange={(e) => setObjectName(e.target.value)}
                       onKeyDown={(e) => e.key === "Enter" && lookup()}
                       placeholder="M42 / NGC 7000 / SN 2024abc…" />
              </label>
              <button onClick={lookup} disabled={resolving}>{resolving ? "…" : "Lookup"}</button>
            </div>
            <div className="row">
              <label>RA (h)<input value={raHours} onChange={(e) => setRaHours(e.target.value)} placeholder="—" /></label>
              <label>Dec (°)<input value={decDeg} onChange={(e) => setDecDeg(e.target.value)} placeholder="—" /></label>
              <label>
                From queue
                <select value="" onChange={(e) => { const b = queue.find((q) => q.id === e.target.value); if (b) loadFromQueue(b); }}>
                  <option value="">pick…</option>
                  {queue.map((b) => (
                    <option key={b.id} value={b.id}>{b.request?.object_name ?? b.id}</option>
                  ))}
                </select>
              </label>
            </div>
          </section>

          <section className="card">
            <h2>Exposure sets</h2>
            <table className="exptable">
              <thead>
                <tr><th>Type</th><th>Filter</th><th>Exp (s)</th><th>Count</th><th>Bin</th><th>Dither px</th><th></th></tr>
              </thead>
              <tbody>
                {recipe.map((r, i) => {
                  const t = r.image_type ?? "LIGHT";
                  const noFilter = t === "BIAS" || t === "DARK";
                  return (
                  <tr key={i}>
                    <td>
                      <select value={t} onChange={(e) => setRow(i, { image_type: e.target.value, ...(e.target.value === "BIAS" ? { exptime_s: 0, dither_px: 0 } : e.target.value !== "LIGHT" ? { dither_px: 0 } : {}) })}>
                        <option value="LIGHT">Light</option>
                        <option value="DARK">Dark</option>
                        <option value="FLAT">Flat</option>
                        <option value="BIAS">Bias</option>
                      </select>
                    </td>
                    <td>
                      <select disabled={noFilter} value={r.filter_slot ?? ""} onChange={(e) => {
                        const v = e.target.value;
                        setRow(i, { filter_slot: v === "" ? null : Number(v), filter_name: v === "" ? "" : filterNames[Number(v) - 1] ?? "" });
                      }}>
                        <option value="">{noFilter ? "(n/a)" : "(current)"}</option>
                        {filterNames.map((nm, k) => (<option key={k} value={k + 1}>{nm}</option>))}
                      </select>
                    </td>
                    <td><input className="cell" disabled={t === "BIAS"} value={t === "BIAS" ? 0 : r.exptime_s} onChange={(e) => setRow(i, { exptime_s: parseFloat(e.target.value) || 0 })} /></td>
                    <td><input className="cell" value={r.count} onChange={(e) => setRow(i, { count: parseInt(e.target.value) || 0 })} /></td>
                    <td><input className="cell" value={r.binning} onChange={(e) => setRow(i, { binning: parseInt(e.target.value) || 1 })} /></td>
                    <td><input className="cell" disabled={t !== "LIGHT"} value={t === "LIGHT" ? r.dither_px : 0} onChange={(e) => setRow(i, { dither_px: parseInt(e.target.value) || 0 })} /></td>
                    <td><button className="small danger" onClick={() => setRecipe((rs) => rs.filter((_, j) => j !== i))} disabled={recipe.length <= 1}>✕</button></td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
            <div className="row">
              <button className="small" onClick={() => setRecipe((rs) => [...rs, blankSet()])}>+ Light</button>
              <button className="small" onClick={() => setRecipe((rs) => [...rs, blankSet("DARK")])}>+ Darks</button>
              <button className="small" onClick={() => setRecipe((rs) => [...rs, blankSet("FLAT")])}>+ Flats</button>
              <button className="small" onClick={() => setRecipe((rs) => [...rs, blankSet("BIAS")])}>+ Bias</button>
              <label className="inline">Repeat<input className="cell" value={repeat} onChange={(e) => setRepeat(e.target.value)} /></label>
              <label className="chk" title="Run an HFR autofocus sweep at the start of the plan"><input type="checkbox" checked={autofocus} onChange={(e) => setAutofocus(e.target.checked)} /> Autofocus at start</label>
              <label className="chk" title="Plate-solve and center on target at the start"><input type="checkbox" checked={center} onChange={(e) => setCenter(e.target.checked)} /> Center at start</label>
              <span className="muted">{totalShots} shots total</span>
            </div>
          </section>

          {err && <div className="err">{err}</div>}
          {msg && <div className="muted" style={{ padding: "0 4px" }}>{msg}</div>}

          <section className="card">
            <div className="row">
              <button onClick={save} disabled={busy}>Save plan</button>
              <button className="active" onClick={() => run(false)} disabled={busy}>Run</button>
              <button onClick={() => run(true)} disabled={busy || !lastBlockId} title={lastBlockId ? "Resume the last run, skipping completed shots" : "Run once first"}>Resume</button>
            </div>
          </section>
        </div>

        {/* saved plans */}
        <div>
          <section className="card">
            <h2>Saved plans <span className="muted">· {plans.length}</span></h2>
            {!plans.length && <div className="muted">no saved plans yet</div>}
            {plans.map((p) => (
              <div className="candrow" key={p.id}>
                <div className="candhead">
                  <b style={{ color: "#fff" }}>{p.name}</b>
                  {p.object_name && <span className="pill idle">{p.object_name}</span>}
                  <span className="muted">{(p.recipe_json ?? []).reduce((a, r) => a + (r.count || 0), 0) * p.repeat} shots</span>
                  {p.last_block_id && <span className="pill ok">run</span>}
                </div>
                <div className="row">
                  <button className="small" onClick={() => loadPlan(p)}>Load</button>
                  <button className="small danger" onClick={() => remove(p.id)}>Delete</button>
                </div>
              </div>
            ))}
          </section>
        </div>
      </div>
    </div>
  );
}
