import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, ApiError, configureClient } from "./api/client";
import type { SessionInfo } from "./api/types";
import { setLanguage } from "./i18n";
import { realtime } from "./realtime";

interface SessionContextValue {
  session: SessionInfo | null;
  loading: boolean;
  setSession: (s: SessionInfo | null) => void;
  logout: () => Promise<void>;
  selectCompanies: (ids: number[], current?: number) => void;
}

const SessionContext = createContext<SessionContextValue | null>(null);

const COMPANY_KEY = "tessera.companies";

function storedCompanies(): { ids: number[]; current: number | null } | null {
  try {
    const raw = window.localStorage.getItem(COMPANY_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSessionState] = useState<SessionInfo | null>(null);
  const [loading, setLoading] = useState(true);

  const setSession = useCallback((s: SessionInfo | null) => {
    if (s) {
      const allowed = new Set(s.allowed_companies.map((c) => c.id));
      const stored = storedCompanies();
      const ids = stored?.ids.filter((id) => allowed.has(id)) ?? [];
      configureClient({
        csrf: s.csrf_token,
        companies: ids.length ? ids : null,
        company: stored?.current && allowed.has(stored.current) ? stored.current : null,
      });
      // before the state update, so the re-render it triggers already uses the user's language
      setLanguage(s.lang || s.user.lang);
      realtime.connect();
    } else {
      configureClient({ csrf: null, companies: null, company: null });
      realtime.disconnect();
    }
    setSessionState(s);
  }, []);

  useEffect(() => {
    configureClient({ onUnauthorized: () => setSession(null) });
    api
      .session()
      .then((s) => setSession(s))
      .catch((e) => {
        if (!(e instanceof ApiError)) console.error(e);
        setSession(null);
      })
      .finally(() => setLoading(false));
  }, [setSession]);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setSession(null);
    }
  }, [setSession]);

  const selectCompanies = useCallback(
    (ids: number[], current?: number) => {
      try {
        window.localStorage.setItem(COMPANY_KEY, JSON.stringify({ ids, current: current ?? ids[0] ?? null }));
      } catch {
        /* private mode: selection lasts for this page only */
      }
      configureClient({ companies: ids, company: current ?? ids[0] ?? null });
      api
        .session()
        .then(setSession)
        .catch(() => setSession(null));
    },
    [setSession],
  );

  const value = useMemo(
    () => ({ session, loading, setSession, logout, selectCompanies }),
    [session, loading, setSession, logout, selectCompanies],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be used inside <SessionProvider>");
  return ctx;
}
