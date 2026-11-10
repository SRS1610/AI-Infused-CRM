import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { configureFormat } from "./kernel/format";
import { t } from "./kernel/i18n";
import "./modules";
import { AppShell, Home } from "./kernel/shell/AppShell";
import { LoginPage } from "./kernel/shell/LoginPage";
import { SessionProvider, useSession } from "./kernel/session";
import { ImportPage } from "./kernel/dataio/ImportPage";
import { InvitePage, PortalApp } from "./modules/portal/PortalApp";
import { QuotePage } from "./modules/portal/QuotePage";
import { ActionView } from "./kernel/views/ActionView";
import { RecordView } from "./kernel/views/RecordView";

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        // client errors are final, except "too many requests": wait as the server asks, then retry
        retry: (count, error: any) =>
          error?.status === 429 ? count < 3 : count < 2 && !(error?.status >= 400 && error?.status < 500),
        retryDelay: (count, error: any) =>
          error?.status === 429 && error.retryAfter
            ? Math.min(error.retryAfter, 30) * 1000
            : Math.min(1000 * 2 ** count, 30_000),
        refetchOnWindowFocus: false,
      },
    },
  });
}

function Routed() {
  const { session, loading } = useSession();
  const location = useLocation();
  useEffect(() => {
    if (session) configureFormat(session.lang, session.user.tz);
  }, [session]);
  if (loading)
    return (
      <div className="splash" role="status">
        {t("Loading Tessera…")}
      </div>
    );
  // public customer pages: token links from emails work without an account
  const token = new URLSearchParams(location.search).get("token");
  if (location.pathname === "/portal/invite") return <InvitePage />;
  if (token && location.pathname.startsWith("/portal/quotes/") && !session?.user.share)
    return (
      <Routes>
        <Route path="/portal/quotes/:id" element={<QuotePage />} />
      </Routes>
    );
  if (!session) return <LoginPage />;
  if (session.user.share) return <PortalApp />;
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/a/:actionRef" element={<ActionView />} />
        <Route path="/m/:model/:id" element={<RecordView />} />
        <Route path="/import/:model" element={<ImportPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AppShell>
  );
}

export function App({ queryClient = createQueryClient() }: { queryClient?: QueryClient }) {
  return (
    <QueryClientProvider client={queryClient}>
      <SessionProvider>
        <BrowserRouter>
          <Routed />
        </BrowserRouter>
      </SessionProvider>
    </QueryClientProvider>
  );
}
