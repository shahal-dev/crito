import { useCallback, useEffect, useRef, useState } from "react";
import { GuideGraph, getJSON } from "./api";

const RA_COLOR = "#5aa9e6"; // RA error
const DEC_COLOR = "#e6a55a"; // Dec error

// PHD2 guiding graph: RA and Dec raw error (pixels) over time on one plot.
export default function GuidingPlot() {
  const ref = useRef<HTMLCanvasElement>(null);
  const data = useRef<GuideGraph | null>(null);
  const [meta, setMeta] = useState<GuideGraph | null>(null);

  const draw = useCallback(() => {
    const c = ref.current;
    if (!c) return;
    const w = Math.max(c.clientWidth || 0, 200);
    const h = 160;
    if (c.width !== w) c.width = w;
    if (c.height !== h) c.height = h;
    const ctx = c.getContext("2d");
    if (!ctx) return;

    ctx.fillStyle = "#05070b";
    ctx.fillRect(0, 0, w, h);

    const g = data.current;
    const samples = g?.samples ?? [];
    const pad = 8;
    let maxAbs = 2;
    for (const s of samples) {
      if (s.ra != null) maxAbs = Math.max(maxAbs, Math.abs(s.ra));
      if (s.dec != null) maxAbs = Math.max(maxAbs, Math.abs(s.dec));
    }
    maxAbs = Math.ceil(maxAbs);
    const mid = h / 2;
    const span = h / 2 - pad;
    const yOf = (v: number) => mid - (v / maxAbs) * span;
    const n = samples.length;
    const xOf = (i: number) => (n <= 1 ? w : (i / (n - 1)) * w);

    // grid: zero line + ±half/±full
    ctx.strokeStyle = "#1a2230";
    ctx.lineWidth = 1;
    [0.5, 1].forEach((f) => {
      ctx.beginPath(); ctx.moveTo(0, mid - f * span); ctx.lineTo(w, mid - f * span); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, mid + f * span); ctx.lineTo(w, mid + f * span); ctx.stroke();
    });
    ctx.strokeStyle = "#2a3850";
    ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(w, mid); ctx.stroke();

    const series = (key: "ra" | "dec", color: string) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      let started = false;
      samples.forEach((s, i) => {
        const v = s[key];
        if (v == null) return;
        const x = xOf(i);
        const y = yOf(v);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      });
      ctx.stroke();
    };
    series("ra", RA_COLOR);
    series("dec", DEC_COLOR);

    ctx.fillStyle = "#6b7a90";
    ctx.font = "10px ui-monospace, monospace";
    ctx.fillText(`±${maxAbs} px`, 4, 12);
    if (!samples.length) {
      ctx.fillStyle = "#6b7a90";
      ctx.fillText(g?.connected ? "waiting for guide steps…" : "PHD2 not connected", w / 2 - 70, mid - 4);
    }
  }, []);

  useEffect(() => {
    let stop = false;
    const tick = () =>
      getJSON<GuideGraph>("/api/guiding/graph?limit=200")
        .then((d) => {
          if (stop) return;
          data.current = d;
          setMeta(d);
          draw();
        })
        .catch(() => {});
    tick();
    const t = setInterval(tick, 1500);
    const onResize = () => draw();
    window.addEventListener("resize", onResize);
    return () => {
      stop = true;
      clearInterval(t);
      window.removeEventListener("resize", onResize);
    };
  }, [draw]);

  return (
    <div>
      <canvas ref={ref} className="guideplot" />
      <div className="row" style={{ gap: 14, fontSize: 11, marginTop: 6 }}>
        <span style={{ color: RA_COLOR }}>■ RA</span>
        <span style={{ color: DEC_COLOR }}>■ Dec</span>
        <span className="muted">{meta?.connected ? `PHD2 ${meta.state}` : "PHD2 disconnected"}</span>
        {meta?.n ? (
          <span className="muted">
            RMS {meta.rms_ra ?? "—"} / {meta.rms_dec ?? "—"} px · {meta.n} steps
          </span>
        ) : null}
      </div>
    </div>
  );
}
