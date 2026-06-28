import { useEffect, useState } from "react";
import { ExposureCalibration, ExposurePlan, ExposureSetup, ExposureSetups, getJSON, post } from "./api";

// SNR the science needs — see docs/exposure-planning.md.
const SNR_PRESETS: { label: string; snr: number; hint: string }[] = [
  { label: "Detect (5σ)", snr: 5, hint: "confident detection" },
  { label: "Photometry 10%", snr: 10, hint: "±0.1 mag" },
  { label: "Photometry 1%", snr: 100, hint: "±0.011 mag" },
  { label: "High SNR", snr: 300, hint: "±0.004 mag" },
];

const num = (s: string): number | null => (s.trim() === "" ? null : Number(s));

export default function ExposurePlanner() {
  const [setups, setSetups] = useState<ExposureSetup[]>([]);
  const [setupId, setSetupId] = useState("");
  const [cal, setCal] = useState<ExposureCalibration | null>(null);
  const [calFile, setCalFile] = useState<string | null>(null);
  const [manual, setManual] = useState(false);

  // science + geometry
  const [mag, setMag] = useState("18.0");
  const [snr, setSnr] = useState("30");
  const [seeing, setSeeing] = useState("3.0");
  const [focal, setFocal] = useState("");
  const [pixel, setPixel] = useState("2.9");
  const [brightest, setBrightest] = useState("");
  const [maxSub, setMaxSub] = useState("");

  // calibration mode
  const [filter, setFilter] = useState("");
  const [gain, setGain] = useState("");
  const [temp, setTemp] = useState("-10");

  // manual mode
  const [readNoise, setReadNoise] = useState("1.1");
  const [fullWell, setFullWell] = useState("13000");
  const [sky, setSky] = useState("8");
  const [zp, setZp] = useState("21");
  const [dark, setDark] = useState("0.01");

  const [plan, setPlan] = useState<ExposurePlan | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Load a calibration table (a setup's own file, or the default). No file → the
  // setup has no measured table yet, so fall back to manual constants.
  const loadCalibration = async (file: string | null) => {
    setCalFile(file);
    if (!file) {
      setCal(null);
      setManual(true);
      return;
    }
    try {
      const c = await getJSON<ExposureCalibration>(`/api/tools/exposure/calibration?file=${encodeURIComponent(file)}`);
      setCal(c);
      setManual(!c.available);
      if (c.available) {
        if (c.filters.length) setFilter(c.filters[0]);
        if (c.gains.length) setGain(String(c.gains[c.gains.length - 1])); // high gain default
        if (c.temps.length) setTemp(String(c.temps[0]));
      }
    } catch {
      setCal(null);
      setManual(true);
    }
  };

  // Apply a setup: auto-fill focal length + pixel size, load its calibration.
  // Fields stay editable afterwards — manual tweaks are always accepted.
  const applySetup = (s: ExposureSetup | undefined) => {
    if (!s) return;
    setSetupId(s.id);
    if (s.focal_length_mm) setFocal(String(s.focal_length_mm));
    if (s.pixel_size_um) setPixel(String(s.pixel_size_um));
    loadCalibration(s.calibration_file);
  };

  useEffect(() => {
    getJSON<ExposureSetups>("/api/tools/setups")
      .then((r) => {
        setSetups(r.setups);
        applySetup(r.setups.find((s) => s.id === r.default_id) ?? r.setups[0]);
      })
      .catch(() => setManual(true));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const compute = async () => {
    setErr(null);
    setBusy(true);
    try {
      const body: Record<string, unknown> = {
        mag: Number(mag),
        required_snr: Number(snr),
        seeing_arcsec: Number(seeing),
        focal_length_mm: num(focal),
        pixel_size_um: num(pixel),
        brightest_mag: num(brightest),
        max_sub_s: num(maxSub),
      };
      if (!manual) {
        body.filter = filter;
        body.gain = num(gain);
        body.temp_c = Number(temp);
        body.calibration_file = calFile;
      } else {
        body.filter = filter || null;
        body.read_noise_e = num(readNoise);
        body.full_well_e = num(fullWell);
        body.sky_e_per_s_per_px = num(sky);
        body.zero_point = num(zp);
        body.dark_e_per_s_per_px = num(dark) ?? 0;
      }
      setPlan((await post("/api/tools/exposure", body)) as ExposurePlan);
    } catch (e) {
      setPlan(null);
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  const fmt = (n: number | null | undefined, d = 1) =>
    n === null || n === undefined ? "∞" : n.toFixed(d);

  return (
    <div className="planpage expplanner">
      <section className="card">
        <h2>
          Exposure planner
          {cal?.camera && <span className="muted" style={{ fontWeight: 400, marginLeft: 8 }}>
            {cal.camera}{cal.sensor ? ` · ${cal.sensor}` : ""}</span>}
          <label className="chk" style={{ float: "right", fontWeight: 400 }}>
            <input type="checkbox" checked={manual} onChange={(e) => setManual(e.target.checked)} />
            manual constants
          </label>
        </h2>

        {/* --- setup selector --- */}
        <div className="row">
          <label className="inline">Setup
            <select className="cell" style={{ width: 240 }} value={setupId}
                    onChange={(e) => applySetup(setups.find((s) => s.id === e.target.value))}>
              {setups.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </label>
          <span className="muted">auto-fills focal length, pixel size & calibration — edit any field freely</span>
        </div>

        {!manual && cal && !cal.available && (
          <div className="warn">no calibration table at <code>{cal.path}</code> — using manual constants.</div>
        )}
        {!manual && cal?.available && (
          <div className="muted" style={{ marginBottom: 6 }}>
            constants from <code>{cal.path}</code> — measure them with{" "}
            <code>python -m crito.calib.characterize</code> for accurate plans.
          </div>
        )}

        {/* --- science --- */}
        <div className="row">
          <label className="inline">Target mag<input className="cell" value={mag} onChange={(e) => setMag(e.target.value)} /></label>
          <label className="inline">Required SNR<input className="cell" value={snr} onChange={(e) => setSnr(e.target.value)} /></label>
          {SNR_PRESETS.map((p) => (
            <button key={p.snr} className="small" title={p.hint} onClick={() => setSnr(String(p.snr))}>{p.label}</button>
          ))}
        </div>

        {/* --- optics --- */}
        <div className="row">
          <label className="inline">Focal length (mm)<input className="cell" value={focal} onChange={(e) => setFocal(e.target.value)} placeholder="required" /></label>
          <label className="inline">Pixel (µm)<input className="cell" value={pixel} onChange={(e) => setPixel(e.target.value)} /></label>
          <label className="inline">Seeing (″)<input className="cell" value={seeing} onChange={(e) => setSeeing(e.target.value)} /></label>
        </div>

        {/* --- sensor: calibration or manual --- */}
        {!manual ? (
          <div className="row">
            <label className="inline">Filter
              <select className="cell" value={filter} onChange={(e) => setFilter(e.target.value)}>
                {(cal?.filters ?? []).map((f) => <option key={f} value={f}>{f}</option>)}
              </select>
            </label>
            <label className="inline">Gain
              <select className="cell" value={gain} onChange={(e) => setGain(e.target.value)}>
                {(cal?.gains ?? []).map((g) => <option key={g} value={g}>{g}</option>)}
              </select>
            </label>
            <label className="inline">Sensor °C
              <select className="cell" value={temp} onChange={(e) => setTemp(e.target.value)}>
                {(cal?.temps ?? []).map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
          </div>
        ) : (
          <div className="row">
            <label className="inline">Filter label<input className="cell" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="optional" /></label>
            <label className="inline">Read noise (e⁻)<input className="cell" value={readNoise} onChange={(e) => setReadNoise(e.target.value)} /></label>
            <label className="inline">Full well (e⁻)<input className="cell" value={fullWell} onChange={(e) => setFullWell(e.target.value)} /></label>
            <label className="inline">Sky (e⁻/s/px)<input className="cell" value={sky} onChange={(e) => setSky(e.target.value)} /></label>
            <label className="inline">Zero point<input className="cell" value={zp} onChange={(e) => setZp(e.target.value)} /></label>
            <label className="inline">Dark (e⁻/s/px)<input className="cell" value={dark} onChange={(e) => setDark(e.target.value)} /></label>
          </div>
        )}

        {/* --- optional constraints --- */}
        <div className="row">
          <label className="inline" title="Brightest field star to protect from saturation (defaults to the target)">
            Brightest star mag<input className="cell" value={brightest} onChange={(e) => setBrightest(e.target.value)} placeholder="optional" /></label>
          <label className="inline" title="Cap the sub length (e.g. tracking/guiding limit)">
            Max sub (s)<input className="cell" value={maxSub} onChange={(e) => setMaxSub(e.target.value)} placeholder="optional" /></label>
          <button className="active" onClick={compute} disabled={busy}>{busy ? "computing…" : "Compute plan"}</button>
        </div>

        {err && <div className="err">{err}</div>}
      </section>

      {plan && (
        <section className="card">
          <h2>Plan <span className={`pill ${plan.snr_achieved >= plan.required_snr ? "ok" : "warn"}`}>{plan.limiting_noise}-limited</span></h2>
          <div className="expplan">
            <div><span>Sub-exposure window</span><b>{fmt(plan.sub_min_s)}s (sky floor) → {fmt(plan.sub_max_s)}{plan.sub_max_s == null ? "" : "s"} (saturation)</b></div>
            <div><span>Recommended sub</span><b>{fmt(plan.sub_recommended_s)} s</b></div>
            <div><span>SNR per sub</span><b>{fmt(plan.snr_per_sub, 1)}</b></div>
            <div className="big"><span>Subs × sub length</span><b>{plan.n_subs} × {fmt(plan.sub_recommended_s)}s</b></div>
            <div className="big"><span>Total integration</span><b>{fmt(plan.total_integration_min)} min</b></div>
            <div><span>Achieved SNR</span><b>{fmt(plan.snr_achieved, 0)} (±{plan.mag_error.toFixed(3)} mag)</b></div>
            <div><span>Source rate</span><b>{plan.source_e_per_s} e⁻/s</b></div>
            <div><span>Sky background</span><b>{plan.sky_e_per_s_per_px} e⁻/s/px</b></div>
            <div><span>Plate scale</span><b>{plan.pixel_scale_arcsec}″/px · FWHM {plan.fwhm_pix}px · {plan.aperture_npix}px aperture</b></div>
          </div>
          {plan.warnings.map((w, i) => <div key={i} className="warn" style={{ marginTop: 6 }}>⚠ {w}</div>)}
        </section>
      )}
    </div>
  );
}
