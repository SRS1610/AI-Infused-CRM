import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { MenuNode } from "../api/types";
import { useHotkeys } from "../hotkeys";
import { Icon, initials, menuIcon } from "../icons";
import { useSession } from "../session";
import { toggleTheme } from "../theme";
import { CommandPalette, flattenMenus } from "./CommandPalette";
import { NotificationBell } from "./NotificationBell";
import { formatDate } from "../format";
import { t } from "../i18n";

const NAV_KEY = "tessera.nav";
const MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

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

/** A colour per app from the eight `--app-N` tokens, by the app's position in the menu. */
export function appColor(index: number): CSSProperties {
  return { "--app-color": `var(--app-${(Math.max(index, 0) % 8) + 1})` } as CSSProperties;
}

export function AppGlyph({ menu, index, size = 20 }: { menu: MenuNode; index: number; size?: number }) {
  return (
    <span className="app-glyph" style={appColor(index)} aria-hidden="true">
      <Icon name={menuIcon(menu)} size={size} />
    </span>
  );
}

/** Names of an app's first menu entries, for the Home tiles. */
function sections(menu: MenuNode, count = 3): string[] {
  const out: string[] = [];
  const walk = (nodes: MenuNode[]) => {
    for (const n of nodes) {
      if (out.length >= count) return;
      if (n.action) out.push(n.name);
      else walk(n.children);
    }
  };
  walk(menu.children);
  return out;
}

function CompanySwitcher() {
  const { session, selectCompanies } = useSession();
  const [open, setOpen] = useState(false);
  if (!session || session.allowed_companies.length < 2)
    return session?.company ? <span className="company">{session.company.name}</span> : null;
  const selected = new Set(session.selected_companies);
  return (
    <div className="dropdown company-switch">
      <button
        type="button"
        className="suite-btn"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((o) => !o)}
      >
        {session.company?.name}
        {selected.size > 1 ? ` +${selected.size - 1}` : ""}
      </button>
      {open && (
        <div className="dropdown-menu right" role="menu" aria-label={t("Companies")}>
          {session.allowed_companies.map((c) => (
            <div key={c.id} className="menu-row">
              <input
                type="checkbox"
                aria-label={t("Include {company}", { company: c.name })}
                checked={selected.has(c.id)}
                onChange={(e) => {
                  const next = e.target.checked ? [...selected, c.id] : [...selected].filter((x) => x !== c.id);
                  if (next.length)
                    selectCompanies(next, next.includes(session.company?.id ?? -1) ? session.company!.id : next[0]);
                }}
              />
              <button
                type="button"
                className="btn ghost small block"
                onClick={() => selectCompanies([c.id, ...[...selected].filter((x) => x !== c.id)], c.id)}
              >
                <span className="spacer" style={{ textAlign: "left" }}>
                  {c.name}
                </span>
                {session.company?.id === c.id && <Icon name="check" size={14} />}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(NAV_KEY) === "collapsed";
  } catch {
    return false;
  }
}

export function AppShell({ children }: { children: ReactNode }) {
  const { session, logout } = useSession();
  const navigate = useNavigate();
  const location = useLocation();
  const online = useOnline();
  const [palette, setPalette] = useState(false);
  const [userMenu, setUserMenu] = useState(false);
  const [launcher, setLauncher] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const menus = useQuery({ queryKey: ["menus", session?.selected_companies.join(",")], queryFn: api.menus });

  const currentAction = decodeURIComponent(location.pathname.match(/^\/a\/([^/?]+)/)?.[1] ?? "");
  const root = findRoot(menus.data ?? [], currentAction);
  const open = (action: string) => {
    setNavOpen(false);
    setLauncher(false);
    navigate(`/a/${encodeURIComponent(action)}`);
  };
  const items = useMemo(() => flattenMenus(menus.data ?? [], open), [menus.data]); // eslint-disable-line react-hooks/exhaustive-deps

  useHotkeys({ "mod+k": () => setPalette(true), escape: () => setLauncher(false) });
  const openHit = useCallback(
    (hit: { model: string; id: number }) => navigate(`/m/${hit.model}/${hit.id}`),
    [navigate],
  );
  const toggleCollapsed = () =>
    setCollapsed((c) => {
      try {
        window.localStorage.setItem(NAV_KEY, c ? "expanded" : "collapsed");
      } catch {
        /* per-browser preference only */
      }
      return !c;
    });
  const hasNav = !!root && root.children.length > 0;

  return (
    <div className="shell">
      <a href="#main" className="skip-link">
        {t("Skip to content")}
      </a>
      <header className="suitebar">
        {hasNav && (
          <button
            type="button"
            className="suite-btn nav-toggle"
            aria-label={t("Open navigation")}
            aria-expanded={navOpen}
            onClick={() => setNavOpen((o) => !o)}
          >
            <Icon name="menu" size={18} />
          </button>
        )}
        <button
          type="button"
          className="suite-btn"
          aria-label={t("App launcher")}
          aria-expanded={launcher}
          aria-controls="app-launcher"
          onClick={() => setLauncher((o) => !o)}
        >
          <Icon name="launcher" size={18} />
        </button>
        <Link to="/" className="brand" aria-label={t("Home")}>
          <img src="/icon.svg" alt="" width={22} height={22} /> <span>Tessera</span>
        </Link>
        {(root || location.pathname === "/") && (
          <>
            <span className="suite-divider" aria-hidden="true" />
            <span className="suite-app">{root?.name ?? t("Home")}</span>
          </>
        )}
        <button
          type="button"
          className="suite-search"
          onClick={() => setPalette(true)}
          aria-label={t("Search menus and records (Ctrl/⌘+K)")}
          aria-keyshortcuts="Control+K Meta+K"
        >
          <Icon name="search" size={16} />
          <span>{t("Search")}</span>
          <kbd>{MAC ? "⌘ K" : "Ctrl K"}</kbd>
        </button>
        <div className="suite-actions">
          <NotificationBell />
          <CompanySwitcher />
          <div className="dropdown">
            <button
              type="button"
              className="suite-btn suite-user"
              aria-label={session?.user.name}
              aria-expanded={userMenu}
              aria-haspopup="menu"
              onClick={() => setUserMenu((o) => !o)}
            >
              <span className="avatar">{initials(session?.user.name)}</span>
            </button>
            {userMenu && (
              <div className="dropdown-menu right" role="menu">
                <div className="menu-heading">
                  <strong>{session?.user.name}</strong>
                  <span>{session?.user.login}</span>
                  {session?.company && <span>{session.company.name}</span>}
                </div>
                <button
                  type="button"
                  role="menuitem"
                  className="menu-item"
                  onClick={() => {
                    toggleTheme();
                    setUserMenu(false);
                  }}
                >
                  <Icon name="moon" />
                  {t("Toggle dark mode")}
                </button>
                <button type="button" role="menuitem" className="menu-item" onClick={() => void logout()}>
                  <Icon name="log-out" />
                  {t("Sign out")}
                </button>
              </div>
            )}
          </div>
        </div>
      </header>
      {launcher && (
        <>
          <div className="launcher-backdrop" onClick={() => setLauncher(false)} aria-hidden="true" />
          <nav id="app-launcher" className="launcher" aria-label={t("Applications")}>
            <h2>{t("Apps")}</h2>
            <ul className="launcher-grid">
              {(menus.data ?? []).map((m, index) => {
                const first = flattenMenus([m], open)[0];
                return (
                  <li key={m.id}>
                    <button
                      type="button"
                      className={`launcher-app${root?.id === m.id ? " current" : ""}`}
                      onClick={() => first?.run()}
                      disabled={!first}
                    >
                      <AppGlyph menu={m} index={index} />
                      {m.name}
                    </button>
                  </li>
                );
              })}
            </ul>
          </nav>
        </>
      )}
      {!online && (
        <div className="offline-banner" role="status">
          {t("You are offline — changes cannot be saved until the connection returns.")}
        </div>
      )}
      <div className="body">
        {hasNav && (
          <nav
            className={`sidebar${navOpen ? " open" : ""}${collapsed ? " collapsed" : ""}`}
            aria-label={t("{app} menu", { app: root.name })}
          >
            <div className="sidebar-head">
              <AppGlyph menu={root} index={(menus.data ?? []).indexOf(root)} size={16} />
              <strong>{root.name}</strong>
              <button
                type="button"
                className="btn ghost small icon-only collapse-btn"
                aria-label={collapsed ? t("Expand navigation") : t("Collapse navigation")}
                title={collapsed ? t("Expand navigation") : t("Collapse navigation")}
                onClick={toggleCollapsed}
              >
                <Icon name={collapsed ? "chevrons-right" : "chevrons-left"} />
              </button>
            </div>
            <div className="sidebar-scroll">
              <MenuTree nodes={root.children} current={currentAction} open={open} />
            </div>
          </nav>
        )}
        <main id="main" className="content" tabIndex={-1}>
          {children}
        </main>
      </div>
      {palette && (
        <CommandPalette
          items={items}
          onClose={() => setPalette(false)}
          searchRecords={session?.modules.includes("web") ? openHit : undefined}
        />
      )}
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
              title={n.name}
              onClick={() => open(n.action!)}
            >
              <Icon name={menuIcon(n)} />
              <span>{n.name}</span>
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
      <div className="home-head">
        <div>
          <h1>{t("Welcome, {name}", { name: session?.user.name })}</h1>
          <p>
            {formatDate(today())}
            {session?.company ? ` · ${session.company.name}` : ""}
          </p>
        </div>
      </div>
      <h2>{t("Apps")}</h2>
      <ul className="app-grid">
        {(menus.data ?? []).map((m, index) => {
          const first = flattenMenus([m], (a) => navigate(`/a/${encodeURIComponent(a)}`))[0];
          const parts = sections(m);
          return (
            <li key={m.id}>
              <button type="button" className="app-tile" onClick={() => first?.run()} disabled={!first}>
                <AppGlyph menu={m} index={index} />
                <span className="app-tile-text">
                  <strong>{m.name}</strong>
                  {parts.length > 1 && <span aria-hidden="true">{parts.join(" · ")}</span>}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
