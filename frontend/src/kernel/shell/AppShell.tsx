import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { MenuNode } from "../api/types";
import { useHotkeys } from "../hotkeys";
import { useSession } from "../session";
import { toggleTheme } from "../theme";
import { CommandPalette, flattenMenus } from "./CommandPalette";

function useOnline(): boolean {
  const [online, setOnline] = useState(typeof navigator === "undefined" ? true : navigator.onLine);
  useEffect(() => {
    const up = () => setOnline(true);
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, []);
  return online;
}

function findRoot(menus: MenuNode[], action: string | undefined): MenuNode | undefined {
  const contains = (m: MenuNode): boolean => m.action === action || m.children.some(contains);
  return action ? menus.find(contains) : undefined;
}

function CompanySwitcher() {
  const { session, selectCompanies } = useSession();
  const [open, setOpen] = useState(false);
  if (!session || session.allowed_companies.length < 2)
    return session?.company ? <span className="company">{session.company.name}</span> : null;
  const selected = new Set(session.selected_companies);
  return (
    <div className="dropdown">
      <button
        type="button"
        className="btn ghost small"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((o) => !o)}
      >
        {session.company?.name}
        {selected.size > 1 ? ` +${selected.size - 1}` : ""}
      </button>
      {open && (
        <div className="dropdown-menu right" role="menu" aria-label="Companies">
          {session.allowed_companies.map((c) => (
            <div key={c.id} className="menu-row">
              <input
                type="checkbox"
                aria-label={`Include ${c.name}`}
                checked={selected.has(c.id)}
                onChange={(e) => {
                  const next = e.target.checked ? [...selected, c.id] : [...selected].filter((x) => x !== c.id);
                  if (next.length)
                    selectCompanies(next, next.includes(session.company?.id ?? -1) ? session.company!.id : next[0]);
                }}
              />
              <button
                type="button"
                className="btn ghost small"
                onClick={() => selectCompanies([c.id, ...[...selected].filter((x) => x !== c.id)], c.id)}
              >
                {c.name}
                {session.company?.id === c.id ? " ✓" : ""}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const { session, logout } = useSession();
  const navigate = useNavigate();
  const location = useLocation();
  const online = useOnline();
  const [palette, setPalette] = useState(false);
  const [userMenu, setUserMenu] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const menus = useQuery({ queryKey: ["menus", session?.selected_companies.join(",")], queryFn: api.menus });

  const currentAction = decodeURIComponent(location.pathname.match(/^\/a\/([^/?]+)/)?.[1] ?? "");
  const root = findRoot(menus.data ?? [], currentAction);
  const open = (action: string) => {
    setNavOpen(false);
    navigate(`/a/${encodeURIComponent(action)}`);
  };
  const items = useMemo(() => flattenMenus(menus.data ?? [], open), [menus.data]); // eslint-disable-line react-hooks/exhaustive-deps

  useHotkeys({ "mod+k": () => setPalette(true) });

  return (
    <div className="shell">
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <header className="topbar">
        <button
          type="button"
          className="btn ghost nav-toggle"
          aria-label="Open navigation"
          aria-expanded={navOpen}
          onClick={() => setNavOpen((o) => !o)}
        >
          ☰
        </button>
        <Link to="/" className="brand" aria-label="Home">
          <img src="/icon.svg" alt="" width={24} height={24} /> <span>Tessera</span>
        </Link>
        <nav className="apps" aria-label="Applications">
          {(menus.data ?? []).map((m) => {
            const first = flattenMenus([m], open)[0];
            return (
              <button
                key={m.id}
                type="button"
                className={`btn ghost small${root?.id === m.id ? " active" : ""}`}
                onClick={() => first?.run()}
              >
                {m.name}
              </button>
            );
          })}
        </nav>
        <div className="spacer" />
        <button
          type="button"
          className="btn ghost small"
          onClick={() => setPalette(true)}
          title="Command palette (Ctrl/⌘+K)"
          aria-keyshortcuts="Control+K Meta+K"
        >
          ⌘K
        </button>
        <CompanySwitcher />
        <div className="dropdown">
          <button
            type="button"
            className="btn ghost small"
            aria-expanded={userMenu}
            aria-haspopup="menu"
            onClick={() => setUserMenu((o) => !o)}
          >
            {session?.user.name}
          </button>
          {userMenu && (
            <div className="dropdown-menu right" role="menu">
              <button type="button" role="menuitem" className="btn ghost block" onClick={() => toggleTheme()}>
                Toggle dark mode
              </button>
              <button type="button" role="menuitem" className="btn ghost block" onClick={() => void logout()}>
                Sign out
              </button>
            </div>
          )}
        </div>
      </header>
      {!online && (
        <div className="offline-banner" role="status">
          You are offline — changes cannot be saved until the connection returns.
        </div>
      )}
      <div className="body">
        {root && root.children.length > 0 && (
          <nav className={`sidebar${navOpen ? " open" : ""}`} aria-label={`${root.name} menu`}>
            <MenuTree nodes={root.children} current={currentAction} open={open} />
          </nav>
        )}
        <main id="main" className="content" tabIndex={-1}>
          {children}
        </main>
      </div>
      {palette && <CommandPalette items={items} onClose={() => setPalette(false)} />}
    </div>
  );
}

function MenuTree({ nodes, current, open }: { nodes: MenuNode[]; current: string; open: (a: string) => void }) {
  return (
    <ul className="menu-tree">
      {nodes.map((n) => (
        <li key={n.id}>
          {n.action ? (
            <button
              type="button"
              className={`menu-link${n.action === current ? " active" : ""}`}
              aria-current={n.action === current ? "page" : undefined}
              onClick={() => open(n.action!)}
            >
              {n.name}
            </button>
          ) : (
            <span className="menu-section">{n.name}</span>
          )}
          {n.children.length > 0 && <MenuTree nodes={n.children} current={current} open={open} />}
        </li>
      ))}
    </ul>
  );
}

export function Home() {
  const menus = useQuery({ queryKey: ["menus"], queryFn: api.menus });
  const navigate = useNavigate();
  const { session } = useSession();
  return (
    <div className="home">
      <h1>Welcome, {session?.user.name}</h1>
      <ul className="app-grid">
        {(menus.data ?? []).map((m) => {
          const first = flattenMenus([m], (a) => navigate(`/a/${encodeURIComponent(a)}`))[0];
          return (
            <li key={m.id}>
              <button type="button" className="app-tile" onClick={() => first?.run()} disabled={!first}>
                <span className="app-icon" aria-hidden="true">
                  {m.name.slice(0, 1)}
                </span>
                {m.name}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
