import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { SessionProvider } from "../session";
import { NotificationBell } from "../shell/NotificationBell";
import { ActivityDashboard } from "./ActivityDashboard";

const session = () => ({
  user: { id: 2, login: "demo", name: "Dana Demo", lang: "en_US", tz: "UTC", share: false },
  allowed_companies: [],
  selected_companies: [],
  groups: [],
  csrf_token: "c",
  lang: "en_US",
  modules: ["chatter"],
});

const ranked = {
  run_id: 31,
  deterministic: true,
  ai_generated: true,
  items: [
    {
      activity_id: 9,
      rank: 1,
      summary: "Negotiate Globex",
      type: "Call",
      category: "call",
      model: "crm.lead",
      res_id: 4,
      res_name: "Globex expansion",
      date_deadline: "2026-11-10",
      score: 70,
      reasons: ["Due today", "Globex expansion is worth 200,000 USD at 50% probability"],
    },
    {
      activity_id: 8,
      rank: 2,
      summary: "Update notes",
      type: "To-do",
      category: "todo",
      model: "crm.lead",
      res_id: 5,
      res_name: null,
      date_deadline: "2026-11-06",
      score: 58,
      reasons: ["Overdue by 4 days"],
    },
  ],
};

const routes = (features: string[] = ["chatter.daily_todo"], available = true) => ({
  "GET /api/v1/auth/session": session,
  "GET /api/v1/ai/status": () => ({
    available,
    features: Object.fromEntries(features.map((f) => [f, { title: f }])),
  }),
  "GET /api/v1/chatter/activities/mine": () => ({ summary: [], items: [] }),
  "GET /api/v1/chatter/ai/todo": () => ranked,
});

function wrap(ui: React.ReactNode, path = "/a/chatter.action_my_activities") {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SessionProvider>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/a/chatter.action_my_activities" element={ui} />
            <Route path="/" element={ui} />
          </Routes>
        </MemoryRouter>
      </SessionProvider>
    </QueryClientProvider>,
  );
}

describe("AI-ranked daily to-do", () => {
  it("ranks the day on demand with reasons, the AI label and feedback, and completes from the list", async () => {
    let done = 0;
    const { calls } = mockFetch({
      ...routes(),
      "POST /api/v1/chatter/activities/9/done": () => {
        done += 1;
        return { ok: true };
      },
      "POST /api/v1/ai/runs/31/feedback": () => ({ id: 1 }),
    });
    wrap(<ActivityDashboard />);
    await userEvent.click(await screen.findByRole("button", { name: "✦ Rank my day" }));
    const panel = await screen.findByRole("region", { name: "AI-ranked to-do" });
    expect(await within(panel).findByText("Negotiate Globex")).toBeInTheDocument();
    expect(within(panel).getByRole("note", { name: /AI-ranked/ })).toBeInTheDocument();
    const rows = within(panel)
      .getAllByRole("listitem")
      .filter((li) => li.parentElement?.tagName === "OL");
    expect(rows.map((r) => within(r).getByText(/Negotiate Globex|Update notes/).textContent)).toEqual([
      "Negotiate Globex",
      "Update notes",
    ]);
    expect(within(rows[0]).getByText("Globex expansion is worth 200,000 USD at 50% probability")).toBeInTheDocument();
    expect(within(rows[0]).getByRole("link", { name: "Globex expansion" })).toHaveAttribute("href", "/m/crm.lead/4");
    await userEvent.click(within(rows[0]).getByRole("button", { name: "Done: Negotiate Globex" }));
    await waitFor(() => expect(done).toBe(1));
    await userEvent.click(within(panel).getByTitle("Helpful"));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/ai/runs/31/feedback")).toBe(true));
  });

  it("is hidden when the feature is off", async () => {
    const { calls } = mockFetch(routes([]));
    wrap(<ActivityDashboard />, "/a/chatter.action_my_activities?ranked=1");
    expect(await screen.findByText("Nothing to do. 🎉")).toBeInTheDocument();
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/ai/status")).toBe(true));
    expect(screen.queryByRole("button", { name: /Rank my day/ })).not.toBeInTheDocument();
    expect(calls.some((c) => c.path === "/api/v1/chatter/ai/todo")).toBe(false);
  });

  it("still works while the AI gateway is down: the ranking is computed by rules", async () => {
    mockFetch(routes(["chatter.daily_todo"], false));
    wrap(<ActivityDashboard />, "/a/chatter.action_my_activities?ranked=1");
    expect(await screen.findByText("Negotiate Globex")).toBeInTheDocument();
    expect(screen.queryByText("AI unavailable")).not.toBeInTheDocument();
  });

  it("opens from the morning notification's link", async () => {
    mockFetch({
      ...routes(),
      "GET /api/v1/models/core.notification": () => ({
        items: [
          {
            id: 5,
            title: "Your AI-ranked to-do for today: 2 activities",
            body: "Start with call: Negotiate Globex",
            kind: "activity",
            model: null,
            res_id: null,
            url: "/a/chatter.action_my_activities?ranked=1",
            is_read: false,
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      }),
      "POST /api/v1/models/core.notification/5/call/action_mark_read": () => ({ result: true }),
    });
    wrap(
      <>
        <NotificationBell />
        <ActivityDashboard />
      </>,
      "/",
    );
    expect(await screen.findByRole("heading", { name: "My activities" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "AI-ranked to-do" })).not.toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Notifications: 1 unread" }));
    await userEvent.click(await screen.findByRole("button", { name: /Your AI-ranked to-do/ }));
    expect(await screen.findByRole("region", { name: "AI-ranked to-do" })).toBeInTheDocument();
    expect(await screen.findByText("Negotiate Globex")).toBeInTheDocument();
  });
});
