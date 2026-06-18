import { useEffect, useRef } from "react";
import { PrecisionTel } from "./api";

// HFR-vs-focuser-position V-curve. Green line = chosen best position.
// Renders at the canvas's real on-screen size (devicePixelRatio-aware) so text
// and the curve stay crisp instead of being stretched from a fixed bitmap.
export default function AutofocusPlot({ af }: { af: PrecisionTel["autofocus"] }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const cv = ref.current;
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth || 320;
    const H = 160;
    cv.width = Math.round(W * dpr);
    cv.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); // draw in CSS pixels
    ctx.clearRect(0, 0, W, H);

    const padL = 38, padR = 12, padT = 14, padB = 26;
    const x0 = padL, x1 = W - padR, y0 = padT, y1 = H - padB;

    // axes
    ctx.strokeStyle = "#243042";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x0, y0); ctx.lineTo(x0, y1); ctx.lineTo(x1, y1);
    ctx.stroke();

    // axis labels
    ctx.fillStyle = "#6b7a90";
    ctx.font = "11px ui-monospace, monospace";
    ctx.textAlign = "center";
    ctx.fillText("focus position", (x0 + x1) / 2, H - 7);
    ctx.save();
    ctx.translate(11, (y0 + y1) / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText("HFR", 0, 0);
    ctx.restore();

    const pts = af.samples.filter((s) => s.hfr != null) as { position: number; hfr: number; stars: number }[];
    if (!pts.length) {
      ctx.fillStyle = "#3a4659";
      ctx.textAlign = "center";
      ctx.fillText("run autofocus to plot HFR", (x0 + x1) / 2, (y0 + y1) / 2);
      return;
    }

    const xs = pts.map((p) => p.position), ys = pts.map((p) => p.hfr);
    let xmin = Math.min(...xs), xmax = Math.max(...xs);
    let ymin = Math.min(...ys), ymax = Math.max(...ys);
    if (xmin === xmax) { xmin -= 1; xmax += 1; }
    const ypad = (ymax - ymin) * 0.12 || 0.5;
    ymin -= ypad; ymax += ypad;
    const sx = (x: number) => x0 + ((x - xmin) / (xmax - xmin)) * (x1 - x0);
    const sy = (y: number) => y1 - ((y - ymin) / (ymax - ymin)) * (y1 - y0);

    // y tick labels (min/max HFR)
    ctx.fillStyle = "#6b7a90";
    ctx.textAlign = "right";
    ctx.fillText(ymax.toFixed(1), x0 - 4, y0 + 4);
    ctx.fillText(ymin.toFixed(1), x0 - 4, y1);

    // best-position marker
    if (af.best_position != null && af.best_position >= xmin && af.best_position <= xmax) {
      ctx.strokeStyle = "#2f9e5a";
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(sx(af.best_position), y0);
      ctx.lineTo(sx(af.best_position), y1);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // curve
    ctx.strokeStyle = "#9ecbff";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(sx(p.position), sy(p.hfr)) : ctx.moveTo(sx(p.position), sy(p.hfr))));
    ctx.stroke();

    // sample points
    ctx.fillStyle = "#c9d4e3";
    pts.forEach((p) => {
      ctx.beginPath();
      ctx.arc(sx(p.position), sy(p.hfr), 3, 0, Math.PI * 2);
      ctx.fill();
    });
  }, [af]);

  return <canvas ref={ref} className="guideplot" style={{ marginTop: 8 }} />;
}
