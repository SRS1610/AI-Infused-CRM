import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { configureFormat } from "./kernel/format";
import { AppShell, Home } from "./kernel/shell/AppShell";
import { LoginPage } from "./kernel/shell/LoginPage";
import { SessionProvider, useSession } from "./kernel/session";
import { ActionView } from "./kernel/views/ActionView";

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        retry: (count, error: any) => count < 2 && !(error?.status >= 400 && error?.status < 500),
        refetchOnWindowFocus: false,
      },
    },
  });
}

function Routed() {
  const { session, loading } = useSession();
  useEffect(() => {
    if (session) configureFormat(session.lang, session.user.tz);
  }, [session]);
  if (loading)
    return (
      <div className="splash" role="status">
        Loading Tessera…
      </div>
    );
  if (!session) return <LoginPage />;
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/a/:actionRef" element={<ActionView />} />
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
