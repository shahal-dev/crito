/**
 * CritoCanvas — D3 SVG brand mark for the login screen.
 *
 * Aesthetic: matches the rest of the site exactly.
 *   • #0b0e14 background
 *   • #243042 grid lines (same as --line)
 *   • #c9d4e3 text (same as --text)
 *   • #e2554f accent line (same as --accent / observatory red)
 *
 * Animation (D3-driven):
 *   1. A clipPath rect sweeps left→right revealing the CRITO text.
 *   2. After text is fully visible, a thin red underline expands from centre.
 *   3. A faint blinking cursor sits at the right edge of the underline.
 *   4. Faint background grid lines (CRT / scope readout aesthetic) drawn with D3.
 *   5. Small scattered star dots around the margins (observatory context).
 */
import { useEffect, useRef } from "react";
import * as d3 from "d3";

const W = 380, H = 108;
const CX = W / 2, CY = H / 2 - 4;

/* Fixed star positions — kept in the margin area so they don't obscure text */
const STARS: [number, number, number][] = [
  [14, 14, 1.5],
  [366, 11, 1.9],
  [22, 90, 1.3],
  [358, 94, 1.7],
  [88, 10, 1.1],
  [300, 98, 1.0],
  [45, 52, 0.9],
  [336, 50, 0.8],
  [180, 6,  0.9],
  [194, 101, 1.1],
  [128, 96, 0.8],
  [252, 9,  0.7],
  [10,  38, 0.8],
  [372, 68, 1.0],
];

/* Targeting-bracket corners (camera finder / telescope reticle) */
const BL = 11; // arm length
type Corner = [number, number, 1 | -1, 1 | -1];
const CORNERS: Corner[] = [
  [14, 10,  1,  1],
  [W - 14, 10, -1,  1],
  [14, H - 10, 1, -1],
  [W - 14, H - 10, -1, -1],
];

export default function CritoCanvas() {
  const ref = useRef<SVGSVGElement>(null);

  useEffect(() => {
    const svg = d3.select(ref.current!);
    svg.selectAll("*").remove();

    /* ── defs ── */
    const defs = svg.append("defs");
    const clipId = `cc-${Math.random().toString(36).slice(2)}`;
    const clip = defs.append("clipPath").attr("id", clipId);
    const clipRect = clip.append("rect")
      .attr("x", 0).attr("y", 0)
      .attr("width", 0).attr("height", H);

    /* ── background ── */
    svg.append("rect")
      .attr("width", W).attr("height", H)
      .attr("fill", "#0b0e14");

    /* ── faint CRT / readout scan lines ── */
    for (let y = 0; y <= H; y += 6) {
      svg.append("line")
        .attr("x1", 0).attr("y1", y).attr("x2", W).attr("y2", y)
        .attr("stroke", "#243042").attr("stroke-width", 0.5).attr("opacity", 0.35);
    }

    /* ── star field (margins only) ── */
    for (const [sx, sy, sr] of STARS) {
      svg.append("circle")
        .attr("cx", sx).attr("cy", sy).attr("r", sr)
        .attr("fill", "#c9d4e3")
        .attr("opacity", 0.18 + Math.random() * 0.15);
    }

    /* ── targeting brackets — observatory red, restrained ── */
    for (const [cx2, cy2, dx, dy] of CORNERS) {
      // horizontal arm
      svg.append("line")
        .attr("x1", cx2).attr("y1", cy2)
        .attr("x2", cx2 + dx * BL).attr("y2", cy2)
        .attr("stroke", "#e2554f").attr("stroke-width", 1).attr("opacity", 0.5);
      // vertical arm
      svg.append("line")
        .attr("x1", cx2).attr("y1", cy2)
        .attr("x2", cx2).attr("y2", cy2 + dy * BL)
        .attr("stroke", "#e2554f").attr("stroke-width", 1).attr("opacity", 0.5);
    }

    /* ── depth shadow text (1 px offset, dark) ── */
    svg.append("text")
      .attr("x", CX + 1).attr("y", CY + 2)
      .attr("text-anchor", "middle").attr("dominant-baseline", "middle")
      .attr("font-family", "ui-monospace, 'SF Mono', Menlo, Consolas, monospace")
      .attr("font-size", 60).attr("font-weight", 900).attr("letter-spacing", 10)
      .attr("fill", "#141a24")
      .text("CRITO");

    /* ── main CRITO text — revealed by sweeping clipPath ── */
    svg.append("text")
      .attr("x", CX).attr("y", CY)
      .attr("text-anchor", "middle").attr("dominant-baseline", "middle")
      .attr("font-family", "ui-monospace, 'SF Mono', Menlo, Consolas, monospace")
      .attr("font-size", 60).attr("font-weight", 900).attr("letter-spacing", 10)
      .attr("fill", "#c9d4e3")
      .attr("clip-path", `url(#${clipId})`)
      .text("CRITO");

    /* ── animate clip rect: left→right reveal ── */
    clipRect.transition()
      .duration(800)
      .ease(d3.easeCubicOut)
      .attr("width", W);

    /* ── red underline sweeps out from centre after text reveals ── */
    const UY = CY + 36;
    const ULEN = 142; // half-length
    const uline = svg.append("line")
      .attr("x1", CX).attr("y1", UY)
      .attr("x2", CX).attr("y2", UY)
      .attr("stroke", "#e2554f").attr("stroke-width", 1)
      .attr("opacity", 0);

    setTimeout(() => {
      uline
        .attr("opacity", 0.7)
        .transition().duration(380).ease(d3.easeCubicOut)
        .attr("x1", CX - ULEN)
        .attr("x2", CX + ULEN);
    }, 760);

    /* ── blinking cursor at right end of underline ── */
    const cursor = svg.append("rect")
      .attr("x", CX + ULEN + 2).attr("y", UY - 7)
      .attr("width", 2).attr("height", 9)
      .attr("fill", "#e2554f")
      .attr("opacity", 0);

    const startBlink = () => {
      cursor.attr("opacity", 0.8);
      const blink = () =>
        cursor.transition().duration(480).attr("opacity", 0)
          .transition().duration(480).attr("opacity", 0.8)
          .on("end", blink);
      blink();
    };
    setTimeout(startBlink, 1200);

    return () => { svg.selectAll("*").remove(); };
  }, []);

  return (
    <svg
      ref={ref}
      viewBox={`0 0 ${W} ${H}`}
      style={{ display: "block", width: "100%", height: "auto" }}
    />
  );
}
