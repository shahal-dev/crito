import { useState } from "react";
import { post, setAuth } from "./api";
import CritoCanvas from "./CritoCanvas";

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
      <div className="loginpanel">

        {/* CASSA logo sits above the card */}
        <img src="/logo.png" className="logo-lg login-logo-above" alt="CASSA" />

        <section className="card loginbox">
          {/* CRITO D3 brand mark inside the card */}
          <div className="login-brand">
            <CritoCanvas />
          </div>

          <div className="muted" style={{ textAlign: "center", marginBottom: 16, marginTop: 4 }}>
            Sign in to the observatory network
          </div>

          <div className="row">
            <label style={{ flex: 1 }}>
              Username
              <input
                id="login-username"
                value={username}
                autoFocus
                autoComplete="username"
                onChange={(e) => setUsername(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && submit()}
              />
            </label>
          </div>
          <div className="row">
            <label style={{ flex: 1 }}>
              Password
              <input
                id="login-password"
                type="password"
                value={password}
                autoComplete="current-password"
                onChange={(e) => setPassword(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && submit()}
              />
            </label>
          </div>
          {err && <div className="err" style={{ marginTop: 12 }}>{err}</div>}
          <div className="row">
            <button
              className="active"
              style={{ flex: 1 }}
              disabled={busy || !username}
              onClick={submit}
            >
              {busy ? "…" : "Sign in"}
            </button>
          </div>
        </section>

      </div>
    </div>
  );
}
