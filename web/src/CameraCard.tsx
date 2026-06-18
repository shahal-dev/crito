import { useState } from "react";
import { CameraTel, mediaUrl, post } from "./api";

function fmt(n: number | null | undefined, d = 1): string {
  return n === null || n === undefined ? "—" : n.toFixed(d);
}

// One self-contained camera panel: status, capture form, and live preview. Used
// for both the science camera (/api/camera) and the guide camera (/api/guide) —
// they expose the same expose/capture/last-image endpoints under different bases.
export default function CameraCard({
  title,
  tel,
  apiBase,
  imgT,
  call,
  refreshArchive,
  filterSlot,
  objectName,
}: {
  title: string;
  tel: CameraTel | null;
  apiBase: string;
  imgT: number;
  call: (fn: () => Promise<unknown>, after?: () => void) => Promise<void>;
  refreshArchive: () => void;
  filterSlot?: number;
  objectName?: string; // taken from the looked-up target, not typed here
}) {
  const [exp, setExp] = useState("2");
  const [imgType, setImgType] = useState("LIGHT");

  return (
    <section className="card">
      <h2>{title}</h2>
      <div className="badges">
        <span className={`pill ${tel?.exposing ? "warn" : "idle"}`}>{tel?.exposing ? "exposing" : "idle"}</span>
        <span className="pill idle">t− {fmt(tel?.exposure_remaining)} s</span>
        {!tel?.connected && <span className="pill bad">not connected</span>}
      </div>
      <div className="row">
        <label>Exp (s)<input value={exp} onChange={(e) => setExp(e.target.value)} /></label>
        <label>Type
          <select value={imgType} onChange={(e) => setImgType(e.target.value)}>
            <option>LIGHT</option><option>DARK</option><option>BIAS</option><option>FLAT</option>
          </select>
        </label>
      </div>
      <div className="row">
        <button
          disabled={!tel?.connected}
          onClick={() => call(
            () => post(`${apiBase}/capture`, {
              seconds: parseFloat(exp), image_type: imgType, object_name: objectName ?? "",
              filter_slot: filterSlot,
            }),
            refreshArchive,
          )}
        >
          Capture &amp; archive
        </button>
        <button disabled={!tel?.connected} onClick={() => call(() => post(`${apiBase}/expose`, { seconds: parseFloat(exp) }))}>
          Quick expose
        </button>
      </div>
      <div className="preview">
        {imgT ? (
          <img src={mediaUrl(`${apiBase}/last-image.png?t=${imgT}`)} alt="last frame" />
        ) : (
          <div className="noimg">no image yet — capture a frame</div>
        )}
      </div>
    </section>
  );
}
