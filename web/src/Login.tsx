import { useState } from "react";
import { post, setAuth } from "./api";

export default function Login({ onLogin }: { onLogin: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setErr(null);
    setBusy(true);
    try {
      const r = (await post("/api/auth/login", { username, password })) as {
        token: string;
        user: { username: string; role: string };
      };
      setAuth(r.token, r.user.role, r.user.username);
      onLogin();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e).replace(/^4\d\d:\s*/, ""));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="loginwrap">
      <section className="card loginbox">
        <img src="/logo.png" className="logo-lg" alt="CASSA" />
        <div className="muted" style={{ marginBottom: 16 }}>Sign in to the observatory network</div>
        <div className="row">
          <label style={{ flex: 1 }}>
            Username
            <input value={username} autoFocus onChange={(e) => setUsername(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && submit()} />
          </label>
        </div>
        <div className="row">
          <label style={{ flex: 1 }}>
            Password
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && submit()} />
          </label>
        </div>
        {err && <div className="err" style={{ marginTop: 12 }}>{err}</div>}
        <div className="row">
          <button className="active" style={{ flex: 1 }} disabled={busy || !username} onClick={submit}>
            {busy ? "…" : "Sign in"}
          </button>
        </div>
      </section>
    </div>
  );
}
