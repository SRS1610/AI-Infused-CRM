import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError } from "../api/client";
import { useSession } from "../session";

export function LoginPage() {
  const { setSession } = useSession();
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [preAuth, setPreAuth] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const providers = useQuery({ queryKey: ["providers"], queryFn: api.providers, retry: false });

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (preAuth) {
        setSession(await api.loginTotp(preAuth, code));
        return;
      }
      const result = await api.login(login, password);
      if ("totp_required" in result) {
        setPreAuth(result.pre_auth_token);
        setPassword("");
      } else {
        setSession(result);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Cannot reach the server");
      if (preAuth && err instanceof ApiError && /expired/i.test(err.message)) setPreAuth(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login">
      <form className="login-card" onSubmit={submit} aria-labelledby="login-title">
        <img src="/icon.svg" alt="" width={48} height={48} />
        <h1 id="login-title">{preAuth ? "Two-factor authentication" : "Sign in to Tessera"}</h1>
        {error && (
          <div className="alert error" role="alert">
            {error}
          </div>
        )}
        {!preAuth ? (
          <>
            <label htmlFor="login">Login</label>
            <input
              id="login"
              className="input"
              autoComplete="username"
              autoFocus
              required
              value={login}
              onChange={(e) => setLogin(e.target.value)}
            />
            <label htmlFor="password">Password</label>
            <input
              id="password"
              className="input"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </>
        ) : (
          <>
            <label htmlFor="code">Authentication code or recovery code</label>
            <input
              id="code"
              className="input"
              inputMode="text"
              autoComplete="one-time-code"
              autoFocus
              required
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
          </>
        )}
        <button type="submit" className="btn primary block" disabled={busy}>
          {busy ? "Signing in…" : preAuth ? "Verify" : "Sign in"}
        </button>
        {!preAuth && (providers.data ?? []).length > 0 && (
          <div className="sso">
            <p className="muted">or continue with</p>
            {providers.data!.map((p) => (
              <a key={p.code} className="btn block" href={`/api/v1/auth/oidc/${encodeURIComponent(p.code)}/start`}>
                {p.name}
              </a>
            ))}
          </div>
        )}
      </form>
    </main>
  );
}
