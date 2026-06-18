import { useEffect, useState } from "react";
import { post, Role, ROLES, ROLE_LABELS, Telemetry } from "./api";

// Common INDI serial baud rates. EQ6-R: 115200 over the mount's built-in USB,
// 9600 via an EQDIR cable — pick the one that matches your wiring.
const BAUDS = ["9600", "19200", "38400", "57600", "115200", "230400"];
const DEFAULT_BAUD = "115200";

export default function Devices({ tel }: { tel: Telemetry | null }) {
  const [ports, setPorts] = useState<Record<string, string>>({});
  const [bauds, setBauds] = useState<Record<string, string>>({});
  // per-role chosen device (before binding); falls back to the bound device or
  // the sole candidate when the user hasn't picked one explicitly.
  const [pick, setPick] = useState<Partial<Record<Role, string>>>({});
  const [host, setHost] = useState("");
  const [portN, setPortN] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const connected = tel?.indi_connected;
  // The device inventory streams live over telemetry, so plugging/unplugging is
  // reflected within a frame — no manual scan needed to notice a dropped device.
  const devs = tel?.devices ?? [];

  // seed the server host/port fields from telemetry once
  useEffect(() => {
    if (tel?.server && !host) {
      setHost(tel.server.host);
      setPortN(String(tel.server.port));
    }
  }, [tel?.server, host]);

  const run = async (fn: () => Promise<unknown>) => {
    setErr(null);
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  const bindRole = (role: Role, device: string) => {
    if (!device) {
      setErr(`pick a device for ${ROLE_LABELS[role]}`);
      return;
    }
    const d = devs.find((x) => x.device === device);
    // For a serial device, apply the port + baud shown in the form before
    // connecting — using the effective values (what the fields display), so the
    // user's baud choice takes effect even if they never edited the port box.
    let params: Record<string, unknown> | undefined;
    if (d?.has_port) {
      const port = ports[device] ?? d.port ?? "";
      const baud = bauds[device] ?? DEFAULT_BAUD;
      params = { DEVICE_BAUD_RATE: { [baud]: true } };
      if (port) params.DEVICE_PORT = { PORT: port };
    }
    run(() => post("/api/devices/bind", { role, device, params }));
  };

  return (
    <section className="card devices">
      <h2>
        Devices
        <span className={`pill ${connected ? "ok" : "bad"}`} style={{ marginLeft: 8 }}>
          INDI {connected ? "connected" : "down"}
        </span>
      </h2>

      {err && <div className="err">{err}</div>}

      <div className="row">
        <label>INDI host<input value={host} onChange={(e) => setHost(e.target.value)} style={{ width: 140 }} /></label>
        <label>port<input value={portN} onChange={(e) => setPortN(e.target.value)} style={{ width: 70 }} /></label>
        <button disabled={busy} onClick={() => run(() => post("/api/indi/server", { host, port: parseInt(portN, 10) }))}>
          Connect server
        </button>
        <button disabled={busy || !connected} onClick={() => run(() => post("/api/indi/rescan"))}>Rescan</button>
        <button disabled={busy || !connected} onClick={() => run(() => post("/api/devices/autodetect"))}>
          Auto-detect &amp; connect all
        </button>
      </div>

      <table className="devtable">
        <thead>
          <tr><th>Role</th><th>Device</th><th>Serial port</th><th>Baud</th><th>State</th><th></th></tr>
        </thead>
        <tbody>
          {ROLES.map((role) => {
            // Every discovered device whose interface advertises this role. A
            // guide camera is just a camera used for guiding, so any device that
            // can act as a camera is eligible — most cameras only report the CCD
            // interface bit, not the separate GUIDER one.
            const candidates = devs.filter(
              (d) => d.roles.includes(role) || (role === "guide" && d.roles.includes("camera")),
            );
            const boundDev = tel?.bindings?.[role] ?? null;
            const boundEntry = boundDev ? devs.find((d) => d.device === boundDev) : undefined;
            // While bound, the dropdown is locked to the bound device; otherwise
            // fall back to an explicit pick, then the sole candidate.
            const selected =
              boundDev ?? pick[role] ?? (candidates.length === 1 ? candidates[0].device : "");
            const selDev = devs.find((d) => d.device === selected);

            // The device this row is about: the bound one if bound, else the pick.
            const rowDev = boundDev ? boundEntry : selDev;
            const present = !!rowDev;                                   // on the bus right now
            const alert = rowDev?.conn_state === "Alert";              // driver flagged an error
            const isConnected = !!rowDev?.connected && !alert;         // healthy, talking

            // Full role status — every state the row can be in:
            //   connected   bound + linked + healthy            -> Disconnect
            //   online      device present, not connected        -> Connect (ready)
            //   offline     device present but driver Alert       -> Connect (retry)
            //   unreachable bound but device gone (unplugged)     -> Connect (disabled)
            //   no device   nothing present for this role         -> Connect (disabled)
            const status =
              boundDev && isConnected ? { label: "connected", cls: "ok" }
              : boundDev && !present ? { label: "unreachable", cls: "bad" }
              : present && alert ? { label: "offline", cls: "bad" }
              : present ? { label: "online", cls: "warn" }
              : { label: "no device", cls: "idle" };
            return (
              <tr key={role}>
                <td><b>{ROLE_LABELS[role]}</b></td>
                <td>
                  {candidates.length ? (
                    <select
                      value={selected}
                      disabled={!!boundDev}
                      onChange={(e) => setPick({ ...pick, [role]: e.target.value })}
                    >
                      <option value="">— none —</option>
                      {candidates.map((d) => (
                        <option key={d.device} value={d.device}>
                          {d.device}
                          {d.bound_as && d.bound_as !== role ? ` (also ${ROLE_LABELS[d.bound_as as Role] ?? d.bound_as})` : ""}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <span className="muted">{connected ? "no device found" : "—"}</span>
                  )}
                </td>
                <td>
                  {selDev?.has_port ? (
                    <input
                      placeholder="/dev/ttyUSB0"
                      value={ports[selDev.device] ?? selDev.port ?? ""}
                      disabled={!!boundDev}
                      onChange={(e) => setPorts({ ...ports, [selDev.device]: e.target.value })}
                      style={{ width: 150 }}
                    />
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td>
                  {selDev?.has_port ? (
                    <select
                      value={bauds[selDev.device] ?? DEFAULT_BAUD}
                      disabled={!!boundDev}
                      onChange={(e) => setBauds({ ...bauds, [selDev.device]: e.target.value })}
                    >
                      {BAUDS.map((b) => <option key={b}>{b}</option>)}
                    </select>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td>
                  <span className={`pill ${status.cls}`}>{status.label}</span>
                </td>
                <td>
                  {boundDev && isConnected ? (
                    // Connected and healthy — the only action is to drop it.
                    <button disabled={busy} className="danger" onClick={() => run(() => post("/api/devices/unbind", { role }))}>
                      Disconnect
                    </button>
                  ) : (
                    // Otherwise offer Connect, clickable only when the target
                    // device is actually present (online/offline) — disabled when
                    // it's unreachable or there's no device at all.
                    <button disabled={busy || !present} onClick={() => bindRole(role, selected)}>Connect</button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
