import { useCallback, useEffect, useMemo, useState } from "react";
import { Candidate, NightInfo, getJSON, post } from "./api";

const PER_PAGE = 20;

const STATE_PILL: Record<string, string> = {
  new: "idle",
  notified: "warn",
  approved_queue: "ok",
  approved_execute: "ok",
  rejected: "bad",
  expired: "idle",
};

// astropy emits ISO without a tz suffix; treat it as UTC, then render in a zone.
function fmtTime(iso: string | null, tz: string): string {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit" });
}

function fmt(n: number | null | undefined, d = 1): string {
  return n === null || n === undefined ? "—" : n.toFixed(d);
}

const toUtcIso = (local: string): string | null => {
  if (!local) return null;
  const d = new Date(local);
  return isNaN(d.getTime()) ? null : d.toISOString();
};

type ActOpts = { exptime?: number; count?: number; scheduled_utc?: string | null; launch?: boolean };
type Act = (id: string, kind: "queue" | "execute" | "reject" | "reset", opts?: ActOpts) => void;

function CandidateRow({ c, busy, act }: { c: Candidate; busy: string | null; act: Act }) {
  const decided = c.state.startsWith("approved") || c.state === "rejected";
  const oid = c.alert_id;
  const [exp, setExp] = useState("120");
  const [shots, setShots] = useState("5");
  const [start, setStart] = useState(""); // datetime-local (browser tz), optional
  const recipe = (): ActOpts => ({ exptime: parseFloat(exp) || 120, count: parseInt(shots) || 1 });
  const off = decided || !c.observable;

  return (
    <div className={`candrow${c.observable ? "" : " dim"}`}>
      <div className="candhead">
        <a className="oid" href={`https://alerce.online/object/${oid}`} target="_blank" rel="noreferrer">
          {oid}
        </a>
        <span className="pill idle">{c.class_label || "unknown"}</span>
        {c.observable ? (
          <span className="pill ok" title="Clears the horizon limit during tonight's dark window">observable</span>
        ) : (
          <span className="pill idle" title={`Peak altitude tonight ${fmt(c.max_alt_deg, 0)}° — below the limit`}>not up tonight</span>
        )}
        <span className={`pill ${STATE_PILL[c.state] ?? "idle"}`}>{c.state.replace("_", " ")}</span>
        <span className="muted">score {fmt(c.score, 2)}</span>
        {c.class_prob != null && <span className="muted">p={fmt(c.class_prob, 2)}</span>}
        {c.decided_by && <span className="muted">· by {c.decided_by}</span>}
      </div>
      <div className="candgrid">
        <span>RA / Dec</span>
        <b>{fmt(c.ra_deg, 3)}, {fmt(c.dec_deg, 3)}</b>
        <span>mag</span>
        <b>{fmt(c.mag, 1)}</b>
        <span>peak alt</span>
        <b>{fmt(c.max_alt_deg, 0)}°</b>
        <span>airmass</span>
        <b>{fmt(c.min_airmass, 2)}</b>
        <span>moon sep</span>
        <b>{fmt(c.moon_sep_deg, 0)}°</b>
        <span>window (BST)</span>
        <b>
          {c.observable
            ? `${fmtTime(c.window_start_utc, "Asia/Dhaka")}–${fmtTime(c.window_end_utc, "Asia/Dhaka")}`
            : "—"}
        </b>
      </div>
      {!decided && (
        <div className="row">
          <label>Exp (s)<input className="cell" value={exp} onChange={(e) => setExp(e.target.value)} /></label>
          <label>Shots<input className="cell" value={shots} onChange={(e) => setShots(e.target.value)} /></label>
          <label>Start time<input type="datetime-local" style={{ width: "auto" }} value={start} onChange={(e) => setStart(e.target.value)} /></label>
        </div>
      )}
      <div className="row">
        <button className="active" disabled={off || busy === c.id + "execute"}
                title={c.observable ? "Start exposing now with these settings" : "Not observable tonight"}
                onClick={() => act(c.id, "execute", { ...recipe(), launch: true })}>Execute now</button>
        <button disabled={off || !start || busy === c.id + "queue"}
                title={start ? "Queue to run at the start time" : "Set a start time first"}
                onClick={() => act(c.id, "queue", { ...recipe(), scheduled_utc: toUtcIso(start) })}>Queue @ time</button>
        <button disabled={off || busy === c.id + "queue"}
                title="Queue without a time — sorted by best observable time"
                onClick={() => act(c.id, "queue", recipe())}>Queue</button>
        {decided ? (
          <button disabled={busy === c.id + "reset"} title="Re-open this candidate"
                  onClick={() => act(c.id, "reset")}>↺ Reset</button>
        ) : (
          <button className="danger" disabled={busy === c.id + "reject"}
                  onClick={() => act(c.id, "reject")}>Reject</button>
        )}
      </div>
    </div>
  );
}

// page-number list with ellipsis: 1 … 4 5 [6] 7 8 … 20
function pagerNumbers(page: number, count: number): (number | "…")[] {
  const out: (number | "…")[] = [];
  for (let i = 0; i < count; i++) {
    if (i === 0 || i === count - 1 || Math.abs(i - page) <= 2) out.push(i);
    else if (out[out.length - 1] !== "…") out.push("…");
  }
  return out;
}

export default function Candidates() {
  const [items, setItems] = useState<Candidate[]>([]);
  const [night, setNight] = useState<NightInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [pollMsg, setPollMsg] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [classFilter, setClassFilter] = useState("all");

  const refresh = useCallback(async () => {
    try {
      const [list, n] = await Promise.all([
        getJSON<Candidate[]>("/api/transient/candidates"),
        getJSON<NightInfo>("/api/transient/night"),
      ]);
      setItems(list);
      setNight(n);
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, [refresh]);

  const classes = useMemo(() => {
    const m = new Map<string, number>();
    for (const c of items) {
      const k = c.class_label || "unknown";
      m.set(k, (m.get(k) ?? 0) + 1);
    }
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [items]);

  const observableCount = useMemo(() => items.filter((c) => c.observable).length, [items]);
  const filtered = useMemo(
    () => (classFilter === "all" ? items : items.filter((c) => (c.class_label || "unknown") === classFilter)),
    [items, classFilter]
  );
  const pageCount = Math.max(1, Math.ceil(filtered.length / PER_PAGE));
  const pageItems = filtered.slice(page * PER_PAGE, page * PER_PAGE + PER_PAGE);

  // keep the page in range as data / filter changes
  useEffect(() => {
    if (page > pageCount - 1) setPage(pageCount - 1);
  }, [pageCount, page]);

  const act: Act = async (id, kind, opts) => {
    setErr(null);
    setBusy(id + kind);
    try {
      if (kind === "reject" || kind === "reset") {
        await post(`/api/transient/candidates/${id}/${kind}`, { actor: "console" });
      } else {
        await post(`/api/transient/candidates/${id}/approve`, {
          action: kind,
          actor: "console",
          recipe: [{ exptime_s: opts?.exptime ?? 120, count: opts?.count ?? 1 }],
          scheduled_utc: opts?.scheduled_utc ?? null,
          launch: opts?.launch ?? false,
        });
      }
      await refresh();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(null);
    }
  };

  const pollNow = async () => {
    setBusy("poll");
    setErr(null);
    setPollMsg(null);
    try {
      const r = (await post("/api/transient/poll")) as { fetched: number; observable: number; error: string | null };
      setPollMsg(
        r.error
          ? `Poll failed: ${r.error}`
          : `Fetched ${r.fetched} alert${r.fetched === 1 ? "" : "s"} · ${r.observable} observable tonight` +
              (r.fetched === 0 ? " — broker returned nothing; try lowering CRITO_ALERCE_PROBABILITY" : "")
      );
      await refresh();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div>
      <section className="card">
        <h2>Tonight</h2>
        {night ? (
          <div className="kv">
            <span>Astronomical dark{night.twilight_used !== -18 ? ` (${night.twilight_used}° twilight)` : ""}</span>
            <b>
              {fmtTime(night.start_utc, "UTC")}–{fmtTime(night.end_utc, "UTC")} UTC ·{" "}
              {fmtTime(night.start_utc, "Asia/Dhaka")}–{fmtTime(night.end_utc, "Asia/Dhaka")} BST
            </b>
          </div>
        ) : (
          <div className="muted">…</div>
        )}
        <div className="row">
          <button onClick={pollNow} disabled={busy === "poll"}>{busy === "poll" ? "polling…" : "Poll ALeRCE now"}</button>
          <button className="small" onClick={refresh}>refresh</button>
          <label>
            Class
            <select value={classFilter} onChange={(e) => { setClassFilter(e.target.value); setPage(0); }}>
              <option value="all">all ({items.length})</option>
              {classes.map(([k, n]) => (
                <option key={k} value={k}>{k} ({n})</option>
              ))}
            </select>
          </label>
          <span className="muted">{items.length} candidate(s) · {observableCount} observable tonight</span>
        </div>
        {busy === "poll" && <div className="muted" style={{ marginTop: 8 }}>polling ALeRCE…</div>}
        {pollMsg && <div className="muted" style={{ marginTop: 8 }}>{pollMsg}</div>}
      </section>

      {err && <div className="err">{err}</div>}

      {filtered.length === 0 && (
        <div className="muted" style={{ padding: "16px 4px" }}>
          No candidates{classFilter !== "all" ? ` in class ${classFilter}` : ""} yet. Click{" "}
          <b>Poll ALeRCE now</b>, or wait for the next automatic poll.
        </div>
      )}

      {pageItems.length > 0 && (
        <section className="card candgroup">
          <h2>
            Candidates <span className="muted">· {filtered.length}{classFilter !== "all" ? ` ${classFilter}` : ""} · page {page + 1}/{pageCount}</span>
          </h2>
          {pageItems.map((c) => (
            <CandidateRow key={c.id} c={c} busy={busy} act={act} />
          ))}
          {pageCount > 1 && (
            <div className="pager">
              <button disabled={page === 0} onClick={() => setPage((p) => p - 1)}>‹ Prev</button>
              {pagerNumbers(page, pageCount).map((p, i) =>
                p === "…" ? (
                  <span key={`e${i}`} className="muted">…</span>
                ) : (
                  <button key={p} className={p === page ? "active" : ""} onClick={() => setPage(p)}>{p + 1}</button>
                )
              )}
              <button disabled={page >= pageCount - 1} onClick={() => setPage((p) => p + 1)}>Next ›</button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
