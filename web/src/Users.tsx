import { useCallback, useEffect, useState } from "react";
import { del, getJSON, getUsername, post } from "./api";

type User = { id: string; username: string; role: string; created_at: string };
const ROLES = ["viewer", "observer", "operator", "admin"];

export default function Users() {
  const [users, setUsers] = useState<User[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [nu, setNu] = useState("");
  const [np, setNp] = useState("");
  const [nr, setNr] = useState("operator");

  const refresh = useCallback(() => {
    getJSON<User[]>("/api/auth/users").then(setUsers).catch((e) => setErr(String(e instanceof Error ? e.message : e)));
  }, []);
  useEffect(() => refresh(), [refresh]);

  const create = async () => {
    setErr(null);
    try {
      await post("/api/auth/users", { username: nu, password: np, role: nr });
      setNu(""); setNp(""); refresh();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  };

  const remove = async (u: User) => {
    setErr(null);
    try {
      await del(`/api/auth/users/${u.id}`);
      refresh();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    }
  };

  const me = getUsername();

  return (
    <div>
      <section className="card">
        <h2>Users <span className="muted">· {users.length}</span></h2>
        {err && <div className="err">{err}</div>}
        <table className="exptable">
          <thead><tr><th>Username</th><th>Role</th><th>Created</th><th></th></tr></thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id}>
                <td><b style={{ color: "#fff" }}>{u.username}</b>{u.username === me ? " (you)" : ""}</td>
                <td><span className="pill idle">{u.role}</span></td>
                <td className="muted">{u.created_at?.slice(0, 10)}</td>
                <td>
                  <button className="small danger" disabled={u.username === me} onClick={() => remove(u)}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card">
        <h2>Add user</h2>
        <div className="row">
          <label>Username<input value={nu} onChange={(e) => setNu(e.target.value)} /></label>
          <label>Password<input type="password" value={np} onChange={(e) => setNp(e.target.value)} /></label>
          <label>Role
            <select value={nr} onChange={(e) => setNr(e.target.value)}>
              {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <button className="active" disabled={!nu || !np} onClick={create}>Create</button>
        </div>
        <div className="muted" style={{ marginTop: 8 }}>
          viewer = read-only · observer = plan/curate · operator = control hardware · admin = manage users
        </div>
      </section>
    </div>
  );
}
