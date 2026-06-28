/**
 * CritoLogo — compact navbar variant of the CRITO brand mark.
 *
 * Same design language as CritoCanvas but sized for a header bar:
 *   • "CRITO" in bold monospace, 22 px
 *   • Small corner targeting brackets in observatory red
 *   • One-shot left→right clip reveal on mount, then static
 *   • No scan lines — too noisy at small size
 */
import { useEffect, useRef } from "react";
import * as d3 from "d3";

const W = 130, H = 38;
const CX = W / 2, CY = H / 2;
const BL = 7; // bracket arm length

type Corner = [number, number, 1 | -1, 1 | -1];
const CORNERS: Corner[] = [
  [6,   4,   1,  1],
  [W-6, 4,  -1,  1],
  [6,   H-4, 1, -1],
  [W-6, H-4,-1, -1],
];

interface Props {
  onClick?: () => void;
  style?: React.CSSProperties;
}

export default function CritoLogo({ onClick, style }: Props) {
  const ref = useRef<SVGSVGElement>(null);

  useEffect(() => {
    const svg = d3.select(ref.current!);
    svg.selectAll("*").remove();

    const defs = svg.append("defs");
    const clipId = `nl-${Math.random().toString(36).slice(2)}`;
    const clip = defs.append("clipPath").attr("id", clipId);
    const clipRect = clip.append("rect")
      .attr("x", 0).attr("y", 0)
      .attr("width", 0).attr("height", H);

    /* transparent bg — nav already has its own background */
    svg.append("rect")
      .attr("width", W).attr("height", H)
      .attr("fill", "transparent");

    /* corner brackets */
    for (const [cx2, cy2, dx, dy] of CORNERS) {
      svg.append("line")
        .attr("x1", cx2).attr("y1", cy2)
        .attr("x2", cx2 + dx * BL).attr("y2", cy2)
        .attr("stroke", "#e2554f").attr("stroke-width", 1).attr("opacity", 0.55);
      svg.append("line")
        .attr("x1", cx2).attr("y1", cy2)
        .attr("x2", cx2).attr("y2", cy2 + dy * BL)
        .attr("stroke", "#e2554f").attr("stroke-width", 1).attr("opacity", 0.55);
    }

    /* depth shadow */
    svg.append("text")
      .attr("x", CX + 1).attr("y", CY + 1)
      .attr("text-anchor", "middle").attr("dominant-baseline", "middle")
      .attr("font-family", "ui-monospace, 'SF Mono', Menlo, Consolas, monospace")
      .attr("font-size", 20).attr("font-weight", 900).attr("letter-spacing", 5)
      .attr("fill", "#141a24")
      .text("CRITO");

    /* main text — revealed by clip sweep */
    svg.append("text")
      .attr("x", CX).attr("y", CY)
      .attr("text-anchor", "middle").attr("dominant-baseline", "middle")
      .attr("font-family", "ui-monospace, 'SF Mono', Menlo, Consolas, monospace")
      .attr("font-size", 20).attr("font-weight", 900).attr("letter-spacing", 5)
      .attr("fill", "#c9d4e3")
      .attr("clip-path", `url(#${clipId})`)
      .text("CRITO");

    /* animate clip once on mount */
    clipRect.transition()
      .duration(700)
      .ease(d3.easeCubicOut)
      .attr("width", W);

    /* thin red underline after reveal */
    const UY = CY + 13;
    const ULEN = 52;
    const uline = svg.append("line")
      .attr("x1", CX).attr("y1", UY)
      .attr("x2", CX).attr("y2", UY)
      .attr("stroke", "#e2554f").attr("stroke-width", 0.8)
      .attr("opacity", 0);

    setTimeout(() => {
      uline.attr("opacity", 0.65)
        .transition().duration(300).ease(d3.easeCubicOut)
        .attr("x1", CX - ULEN).attr("x2", CX + ULEN);
    }, 660);

    return () => { svg.selectAll("*").remove(); };
  }, []);

  return (
    <svg
      ref={ref}
      viewBox={`0 0 ${W} ${H}`}
      style={{
        display: "block",
        height: 38,
        width: "auto",
        cursor: onClick ? "pointer" : undefined,
        flexShrink: 0,
        ...style,
      }}
      onClick={onClick}
    />
  );
}
