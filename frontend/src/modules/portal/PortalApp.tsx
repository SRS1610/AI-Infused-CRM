import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, Navigate, Route, Routes, useNavigate, useSearchParams } from "react-router-dom";
import { request } from "../../kernel/api/client";
import { formatMonetary } from "../../kernel/format";
import { t, tx } from "../../kernel/i18n";
import { useSession } from "../../kernel/session";
import { QuotePage, quoteStateLabel } from "./QuotePage";

interface Doc {
  id: number;
  name: string;
  date_order: string | null;
  validity_date: string | null;
  amount_total: number;
  currency: string;
  state: string;
}

/** The whole client for portal (customer) users. */
export function PortalApp() {
  const { session, logout } = useSession();
  return (
    <div className="portal-shell">
      <a href="#main" className="skip-link">
        {t("Skip to content")}
      </a>
      <header className="topbar">
        <Link to="/portal" className="brand">
          <img src="/icon.svg" alt="" width={24} height={24} />{" "}
          <span>{session?.company?.name ?? t("Customer portal")}</span>
        </Link>
        <nav className="apps" aria-label={t("Portal")}>
          <Link className="btn ghost small" to="/portal">
            {t("My documents")}
          </Link>
          <Link className="btn ghost small" to="/portal/profile">
            {t("My details")}
          </Link>
        </nav>
        <div className="spacer" />
        <span className="muted small">{session?.user.name}</span>
        <button type="button" className="btn ghost small" onClick={() => void logout()}>
          {t("Sign out")}
        </button>
      </header>
      <main id="main" className="content" tabIndex={-1}>
        <Routes>
          <Route path="/portal" element={<Documents />} />
          <Route path="/portal/profile" element={<Profile />} />
          <Route path="/portal/quotes/:id" element={<QuotePage />} />
          <Route path="*" element={<Navigate to="/portal" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function Documents() {
  const navigate = useNavigate();
  const docs = useQuery({
    queryKey: ["portal-docs"],
    queryFn: () => request<{ quotations: Doc[] }>("GET", "/portal/documents"),
  });
  return (
    <div className="portal-page">
      <h1>{t("My documents")}</h1>
      <h2>{t("Quotations")}</h2>
      {docs.isLoading && <p className="muted">{t("Loading…")}</p>}
      {docs.data && docs.data.quotations.length === 0 && <p className="muted">{t("No quotations yet.")}</p>}
      {docs.data && docs.data.quotations.length > 0 && (
        <table className="pivot-table">
          <thead>
            <tr>
              <th scope="col">{t("Number")}</th>
              <th scope="col">{t("Date")}</th>
              <th scope="col">{t("Valid until")}</th>
              <th scope="col" className="num">
                {t("Total")}
              </th>
              <th scope="col">{t("Status")}</th>
            </tr>
          </thead>
          <tbody>
            {docs.data.quotations.map((d) => (
              <tr key={d.id} className="clickable" onClick={() => navigate(`/portal/quotes/${d.id}`)}>
                <th scope="row">
                  <Link to={`/portal/quotes/${d.id}`}>{d.name}</Link>
                </th>
                <td>{d.date_order?.slice(0, 10)}</td>
                <td>{d.validity_date}</td>
                <td className="num">{formatMonetary(d.amount_total, d.currency)}</td>
                <td>
                  <span className={`badge state-${d.state}`}>{quoteStateLabel(d.state)}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** Editable contact details: the API field and its label in the current language. */
const profileFields = () =>
  [
    ["phone", t("Phone")],
    ["mobile", t("Mobile")],
    ["function", t("Job position")],
    ["street", t("Street")],
    ["street2", t("Street (2)")],
    ["zip", t("Postcode")],
    ["city", t("City")],
  ] as const;

function Profile() {
  const queryClient = useQueryClient();
  const me = useQuery({ queryKey: ["portal-me"], queryFn: () => request<Record<string, any>>("GET", "/portal/me") });
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);
  if (!me.data) return <p className="muted">{t("Loading…")}</p>;
  return (
    <div className="portal-page">
      <h1>{t("My details")}</h1>
      <p>
        <strong>{me.data.name}</strong>
        {me.data.company && <> · {me.data.company}</>} · {me.data.email}
      </p>
      <form
        className="portal-form"
        onSubmit={async (e) => {
          e.preventDefault();
          const updated = await request<Record<string, any>>("PATCH", "/portal/me", { body: draft });
          queryClient.setQueryData(["portal-me"], updated);
          setDraft({});
          setSaved(true);
        }}
      >
        {profileFields().map(([key, label]) => (
          <label key={key}>
            {label}
            <input
              className="input"
              value={draft[key] ?? me.data![key] ?? ""}
              onChange={(e) => {
                setSaved(false);
                setDraft((d) => ({ ...d, [key]: e.target.value }));
              }}
            />
          </label>
        ))}
        <button type="submit" className="btn primary" disabled={!Object.keys(draft).length}>
          {t("Save")}
        </button>
        {saved && (
          <span role="status" className="muted">
            {t("Saved.")}
          </span>
        )}
      </form>
    </div>
  );
}

/** /portal/invite?token=… — choose a password, then sign in. */
export function InvitePage() {
  const [params] = useSearchParams();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="login-page">
      <form
        className="login-card"
        onSubmit={async (e) => {
          e.preventDefault();
          if (password !== confirm) {
            setError(t("The passwords do not match"));
            return;
          }
          try {
            const out = await request<{ login: string }>("POST", "/portal/invite/accept", {
              body: { token: params.get("token"), password },
            });
            setDone(out.login);
          } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
          }
        }}
      >
        <h1>{t("Welcome")}</h1>
        {done ? (
          <p role="status">
            {tx("Your account is ready. {link}.", {
              link: <a href="/">{t("Sign in as {login}", { login: done })}</a>,
            })}
          </p>
        ) : (
          <>
            <label>
              {t("Choose a password")}
              <input
                className="input"
                type="password"
                autoComplete="new-password"
                minLength={8}
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
            <label>
              {t("Repeat it")}
              <input
                className="input"
                type="password"
                autoComplete="new-password"
                required
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </label>
            <button type="submit" className="btn primary">
              {t("Activate my account")}
            </button>
          </>
        )}
        {error && (
          <div className="alert error" role="alert">
            {error}
          </div>
        )}
      </form>
    </div>
  );
}
