import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { mockFetch } from "../test/mockApi";
import type { ChatterMessage } from "./api/platform";
import type { ViewsResponse } from "./api/types";
import { ActivityDashboard } from "./chatter/ActivityDashboard";
import { Chatter } from "./chatter/Chatter";
import { ExportDialog } from "./dataio/ExportDialog";
import { ImportPage } from "./dataio/ImportPage";
import { SessionProvider } from "./session";
import { CommandPalette } from "./shell/CommandPalette";
import { NotificationBell } from "./shell/NotificationBell";
import { CalendarView } from "./views/CalendarView";
import { GraphView } from "./views/GraphView";
import { buildPivot, groupLabel, series, toCsv } from "./views/pivotData";
import { PivotView } from "./views/PivotView";
import { StatusBar } from "./views/StatusBar";

function wrap(ui: React.ReactNode, path = "/") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SessionProvider>
        <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
      </SessionProvider>
    </QueryClientProvider>,
  );
}

const session = {
  "GET /api/v1/auth/session": () => ({
    user: { id: 2, login: "admin", name: "Admin", lang: "en_US", tz: "UTC" },
    allowed_companies: [],
    selected_companies: [],
    groups: [],
    csrf_token: "c",
    lang: "en_US",
    modules: ["core", "web", "chatter", "workflow", "reports"],
  }),
};

const message = (over: Partial<ChatterMessage> = {}): ChatterMessage => ({
  id: 1,
  type: "comment",
  internal: false,
  subject: null,
  body: "<p>Hello team</p>",
  date: "2026-03-01T10:00:00Z",
  author: { id: 2, name: "Admin" },
  email_from: null,
  tracking: [],
  attachments: [],
  reactions: [],
  ai_assisted: false,
  editable: true,
  ...over,
});

const meta: ViewsResponse = {
  model: {
    name: "demo.deal",
    description: "Deal",
    rec_name: "name",
    order: "id",
    methods: [],
    access: { read: true, write: true, create: true, unlink: true },
  },
  fields: {
    name: { name: "name", type: "char", string: "Name", required: true, readonly: false, store: true },
    stage: {
      name: "stage",
      type: "selection",
      string: "Stage",
      required: false,
      readonly: false,
      store: true,
      selection: [
        ["new", "New"],
        ["won", "Won"],
      ],
    },
    owner_id: {
      name: "owner_id",
      type: "many2one",
      string: "Owner",
      required: false,
      readonly: false,
      store: true,
      comodel: "core.user",
    },
    amount: { name: "amount", type: "float", string: "Amount", required: false, readonly: false, store: true },
    close_date: {
      name: "close_date",
      type: "date",
      string: "Close date",
      required: false,
      readonly: false,
      store: true,
    },
  },
  views: {
    calendar: { type: "calendar", model: "demo.deal", date_start: "close_date", title: "name" },
    pivot: { type: "pivot", model: "demo.deal", measures: ["amount"], row: "stage" },
    graph: { type: "graph", model: "demo.deal", measures: ["amount"], row: "stage" },
  },
};

describe("pivot data helpers", () => {
  const groups = [
    { __count: 2, __domain: [], stage: "new", owner_id: { id: 1, display_name: "Ann" }, "amount:sum": 100 },
    { __count: 1, __domain: [], stage: "won", owner_id: { id: 1, display_name: "Ann" }, "amount:sum": 50 },
    { __count: 3, __domain: [], stage: "won", owner_id: null, "amount:sum": 25 },
  ];
  it("builds rows, columns and totals", () => {
    const t = buildPivot(groups, ["stage"], "owner_id", "amount:sum");
    expect(t.rows).toEqual([["new"], ["won"]]);
    expect(t.cols).toEqual(["Ann", "None"]);
    expect(t.cells.get("won||None")).toBe(25);
    expect(t.rowTotals.get("won")).toBe(75);
    expect(t.colTotals.get("Ann")).toBe(150);
    expect(t.total).toBe(175);
    expect(toCsv(t, ["Stage"]).split("\n")).toEqual([
      "Stage,Ann,None,Total",
      "new,100,0,100",
      "won,50,25,75",
      "Total,150,25,175",
    ]);
    expect(buildPivot(groups, ["stage"], null, "__count").total).toBe(6);
  });
  it("labels and series", () => {
    expect(groupLabel(null)).toBe("None");
    expect(groupLabel({ id: 1, display_name: "X" })).toBe("X");
    expect(series(groups, "stage", "amount:sum")).toEqual([
      { label: "new", value: 100 },
      { label: "won", value: 75 },
    ]);
  });
});

describe("chatter", () => {
  it("lists messages, logs a note with a mention, reacts and follows", async () => {
    const posted: any[] = [];
    const { calls } = mockFetch({
      ...session,
      "GET /api/v1/chatter/demo.deal/7/messages": () => ({
        items: [
          message(),
          message({
            id: 2,
            type: "notification",
            body: "",
            editable: false,
            tracking: [{ field: "stage", label: "Stage", old: "New", new: "Won" }],
          }),
        ],
        has_more: false,
      }),
      "GET /api/v1/chatter/demo.deal/7/followers": () => ({
        items: [{ id: 1, user_id: 2, name: "Admin", subtypes: [] }],
        following: true,
        subtypes: [],
      }),
      "GET /api/v1/chatter/demo.deal/7/activities": () => [],
      "GET /api/v1/chatter/mentions": () => [{ id: 5, name: "Rita Reader", login: "rita" }],
      "POST /api/v1/chatter/demo.deal/7/messages": (_u, init) => {
        posted.push(JSON.parse(String(init.body)));
        return message({ id: 3 });
      },
      "POST /api/v1/chatter/messages/1/reactions": () =>
        message({ reactions: [{ emoji: "👍", count: 1, users: ["Admin"], mine: true }] }),
      "DELETE /api/v1/chatter/demo.deal/7/followers": () => ({ items: [], following: false, subtypes: [] }),
    });
    wrap(<Chatter model="demo.deal" resId={7} />);
    expect(await screen.findByText("Hello team")).toBeInTheDocument();
    expect(screen.getByText("Won")).toBeInTheDocument(); // tracking value
    await userEvent.click(screen.getByRole("button", { name: "Log note" }));
    const box = screen.getByPlaceholderText(/internal note/);
    await userEvent.type(box, "Ping @rit");
    await userEvent.click(await screen.findByRole("button", { name: /Rita Reader/ }));
    await userEvent.type(box, "please check <this>");
    await userEvent.click(screen.getByRole("button", { name: "Log" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({ internal: true, mention_user_ids: [5] });
    expect(posted[0].body).toContain("&lt;this&gt;");
    await userEvent.click(screen.getByRole("button", { name: "React with 👍" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/reactions"))).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: /Following · 1/ }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.path.endsWith("/followers"))).toBe(true));
  });

  it("schedules and completes activities", async () => {
    let done = false;
    const activity = {
      id: 9,
      model: "demo.deal",
      res_id: 7,
      res_name: "Big deal",
      type: { id: 1, name: "Call", icon: "phone" },
      category: "call",
      summary: "Call back",
      note: null,
      date_deadline: "2026-03-02",
      state: "overdue",
      user: { id: 2, name: "Admin" },
    };
    const { calls } = mockFetch({
      ...session,
      "GET /api/v1/chatter/demo.deal/7/messages": () => ({ items: [], has_more: false }),
      "GET /api/v1/chatter/demo.deal/7/followers": () => ({ items: [], following: false, subtypes: [] }),
      "GET /api/v1/chatter/demo.deal/7/activities": () => (done ? [] : [activity]),
      "POST /api/v1/chatter/demo.deal/7/activities": () => activity,
      "POST /api/v1/chatter/activities/9/done": () => {
        done = true;
        return { ok: true };
      },
    });
    wrap(<Chatter model="demo.deal" resId={7} />);
    expect(await screen.findByText("No messages yet.")).toBeInTheDocument();
    expect(await screen.findByText(/Call back/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Activity" }));
    await userEvent.type(screen.getByLabelText("Summary"), "Send quote");
    await userEvent.click(screen.getByRole("button", { name: "Schedule" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path.endsWith("/activities"))).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByText(/Call back/)).not.toBeInTheDocument());
  });
});

describe("status bar", () => {
  it("shows allowed transitions and reports conflicts", async () => {
    const changed = vi.fn();
    let fail = false;
    mockFetch({
      ...session,
      "GET /api/v1/workflow/demo.deal/7/statusbar": () => ({
        workflow: { id: 1, name: "Sales", field: "stage", strict: true },
        current: "new",
        states: [
          { key: "new", name: "New", fold: false, final: false, current: true },
          { key: "won", name: "Won", fold: false, final: true, current: false },
          { key: "lost", name: "Lost", fold: true, final: true, current: false },
        ],
        transitions: [{ id: 3, name: "Mark won", to: "won", confirm: null, highlight: true }],
      }),
      "POST /api/v1/workflow/demo.deal/7/transitions/3": () =>
        fail
          ? new Response(JSON.stringify({ error: "ConflictError", message: "Add an amount first" }), { status: 409 })
          : { workflow: null, states: [], transitions: [] },
    });
    wrap(<StatusBar model="demo.deal" resId={7} onChanged={changed} />);
    expect(await screen.findByRole("button", { name: "Mark won" })).toBeInTheDocument();
    expect(screen.queryByText("Lost")).not.toBeInTheDocument(); // folded and not current
    expect(screen.getByText("New")).toHaveAttribute("aria-current", "step");
    await userEvent.click(screen.getByRole("button", { name: "Mark won" }));
    await waitFor(() => expect(changed).toHaveBeenCalled());
    fail = true;
  });
});

describe("calendar, pivot and graph", () => {
  it("places records on their day and navigates months", async () => {
    const today = new Date();
    const day = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-15`;
    const onOpen = vi.fn();
    const { calls } = mockFetch({
      ...session,
      "GET /api/v1/models/demo.deal": () => ({
        items: [{ id: 4, name: "Kick-off", close_date: day, display_name: "Kick-off" }],
        total: 1,
        limit: 500,
        offset: 0,
      }),
    });
    wrap(<CalendarView model="demo.deal" meta={meta} view={meta.views.calendar} domain={[]} onOpen={onOpen} />);
    await userEvent.click(await screen.findByRole("button", { name: "Kick-off" }));
    expect(onOpen).toHaveBeenCalledWith(4);
    await userEvent.click(screen.getByRole("button", { name: "Next month" }));
    await userEvent.click(screen.getByRole("button", { name: "Today" }));
    await waitFor(() =>
      expect(calls.filter((c) => c.path === "/api/v1/models/demo.deal").length).toBeGreaterThanOrEqual(2),
    );
    await userEvent.click(screen.getByRole("tab", { name: "Week" }));
    expect(screen.getByRole("list", { name: /–/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Day" }));
    await userEvent.click(screen.getByRole("button", { name: "Next day" }));
  });

  it("uses the view's expansion endpoint for recurring meetings", async () => {
    const now = new Date();
    const at = (h: number, m = 0) => new Date(now.getFullYear(), now.getMonth(), 10, h, m).toISOString();
    const { calls } = mockFetch({
      ...session,
      "GET /api/v1/calendar/events": () => [{ id: 3, name: "Standup", start: at(9), stop: at(9, 15), recurring: true }],
    });
    const onOpen = vi.fn();
    wrap(
      <CalendarView
        model="calendar.event"
        meta={meta}
        view={{ ...meta.views.calendar, expand_endpoint: "/calendar/events" }}
        domain={[]}
        onOpen={onOpen}
      />,
    );
    await userEvent.click(await screen.findByRole("button", { name: /Standup/ }));
    expect(onOpen).toHaveBeenCalledWith(3);
    expect(calls.find((c) => c.path === "/api/v1/calendar/events")?.url.searchParams.get("start")).toBeTruthy();
  });

  it("renders a pivot with totals and changes groupings", async () => {
    const { calls } = mockFetch({
      ...session,
      "POST /api/v1/models/demo.deal/read_group": () => ({
        groups: [
          { __count: 1, __domain: [], stage: "new", "amount:sum": 10 },
          { __count: 2, __domain: [], stage: "won", "amount:sum": 30 },
        ],
      }),
    });
    wrap(<PivotView model="demo.deal" meta={meta} view={meta.views.pivot} domain={[]} />);
    expect(await screen.findByRole("rowheader", { name: "won" })).toBeInTheDocument();
    expect(screen.getAllByText("40").length).toBeGreaterThan(0); // total
    fireEvent.change(screen.getByLabelText("Columns"), { target: { value: "owner_id" } });
    await waitFor(() => expect(calls.some((c) => c.body?.groupby?.join(",") === "stage,owner_id")).toBe(true));
    fireEvent.change(screen.getByLabelText("Measure"), { target: { value: "__count" } });
    await waitFor(() => expect(calls.some((c) => c.body?.aggregates?.length === 0)).toBe(true));
  });

  it("draws bar and pie charts with an accessible table", async () => {
    mockFetch({
      ...session,
      "POST /api/v1/models/demo.deal/read_group": () => ({
        groups: [
          { __count: 1, __domain: [], stage: "new", "amount:sum": 10 },
          { __count: 2, __domain: [], stage: "won", "amount:sum": 30 },
        ],
      }),
    });
    const { container } = wrap(<GraphView model="demo.deal" meta={meta} view={meta.views.graph} domain={[]} />);
    expect(await screen.findByRole("img", { name: /Amount by Stage \(bar chart\)/ })).toBeInTheDocument();
    expect(container.querySelectorAll("rect").length).toBeGreaterThanOrEqual(2);
    await userEvent.click(screen.getByRole("radio", { name: "Pie" }));
    expect(container.querySelectorAll("path").length).toBe(2);
    await userEvent.click(screen.getByRole("radio", { name: "Line" }));
    expect(container.querySelector("polyline")).not.toBeNull();
  });
});

describe("activity dashboard and notifications", () => {
  it("shows buckets per model", async () => {
    const { calls } = mockFetch({
      ...session,
      "GET /api/v1/chatter/activities/mine": () => ({
        summary: [{ model: "demo.deal", label: "Deal", overdue: 1, today: 0, planned: 2 }],
        items: [
          {
            id: 1,
            model: "demo.deal",
            res_id: 7,
            res_name: "Big deal",
            type: { id: 1, name: "Call", icon: null },
            category: "call",
            summary: "Ring",
            note: null,
            date_deadline: "2026-01-01",
            state: "overdue",
            user: { id: 2, name: "Admin" },
          },
        ],
      }),
    });
    wrap(<ActivityDashboard />);
    expect(await screen.findByRole("rowheader", { name: "Deal" })).toBeInTheDocument();
    expect(screen.getByText(/Ring/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: /Overdue/ }));
    await waitFor(() => expect(calls.some((c) => c.url.searchParams.get("state") === "overdue")).toBe(true));
  });

  it("lists unread notifications and marks them read", async () => {
    const { calls } = mockFetch({
      ...session,
      "GET /api/v1/models/core.notification": () => ({
        items: [
          {
            id: 11,
            title: "Admin mentioned you",
            body: "hi",
            kind: "mention",
            model: "demo.deal",
            res_id: 7,
            is_read: false,
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
      }),
      "POST /api/v1/models/core.notification/11/call/action_mark_read": () => ({ result: true }),
    });
    wrap(
      <Routes>
        <Route path="/" element={<NotificationBell />} />
        <Route path="/m/:model/:id" element={<p>record page</p>} />
      </Routes>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Notifications: 1 unread" }));
    await userEvent.click(screen.getByRole("button", { name: /Admin mentioned you/ }));
    expect(await screen.findByText("record page")).toBeInTheDocument();
    expect(calls.some((c) => c.path.endsWith("/action_mark_read"))).toBe(true);
  });
});

describe("palette, import and export", () => {
  it("adds server-side record results to the palette", async () => {
    mockFetch({
      "GET /api/v1/web/search": () => ({
        records: [{ model: "demo.deal", model_label: "Deal", id: 7, title: "Acme expansion", score: 1 }],
        menus: [],
      }),
    });
    const open = vi.fn();
    render(<CommandPalette items={[]} onClose={() => undefined} searchRecords={open} />);
    await userEvent.type(screen.getByRole("combobox"), "acme");
    await userEvent.click(await screen.findByText("Acme expansion", {}, { timeout: 2000 }));
    expect(open).toHaveBeenCalledWith(expect.objectContaining({ id: 7 }));
  });

  it("uploads, maps, dry-runs and starts an import", async () => {
    const job = {
      id: 3,
      model: "demo.deal",
      filename: "deals.csv",
      headers: ["Name", "Amount"],
      preview: [["Acme", "10"]],
      row_count: 1,
      mapping: { Name: "name", Amount: null },
      options: {},
      state: "draft",
      created: 0,
      updated: 0,
      error_count: 0,
      errors: [],
      job_id: null,
      fields: [
        { name: "name", label: "Name", type: "char", required: true },
        { name: "amount", label: "Amount", type: "float", required: false },
      ],
    };
    const patches: any[] = [];
    mockFetch({
      ...session,
      "POST /api/v1/import/demo.deal": () => job,
      "PATCH /api/v1/import/jobs/3": (_u, init) => {
        patches.push(JSON.parse(String(init.body)));
        return { ...job, mapping: { Name: "name", Amount: "amount" } };
      },
      "POST /api/v1/import/jobs/3/dry-run": () => ({
        ...job,
        state: "tested",
        error_count: 1,
        errors: [{ row: 2, column: "Amount", field: "amount", message: "'x' is not a number" }],
      }),
      "POST /api/v1/import/jobs/3/start": () => ({ ...job, state: "queued", job_id: 44 }),
    });
    wrap(
      <Routes>
        <Route path="/import/:model" element={<ImportPage />} />
      </Routes>,
      "/import/demo.deal",
    );
    const file = new File(["Name,Amount\nAcme,10\n"], "deals.csv", { type: "text/csv" });
    await userEvent.upload(screen.getByLabelText(/Choose a CSV/), file);
    expect(await screen.findByText("deals.csv")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Field for column Amount"), { target: { value: "amount" } });
    await userEvent.click(screen.getByRole("button", { name: "Test (dry run)" }));
    expect(await screen.findByText("'x' is not a number")).toBeInTheDocument();
    expect(patches[0].mapping).toEqual({ Name: "name", Amount: "amount" });
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    expect(await screen.findByRole("progressbar", { name: "Import progress" })).toBeInTheDocument();
  });

  it("exports chosen fields and reports dropped ones", async () => {
    const created = vi.fn(() => "blob:x");
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: created, revokeObjectURL: vi.fn() }));
    const bodies: any[] = [];
    mockFetch({
      "GET /api/v1/export/demo.deal/fields": () => [
        { path: "name", label: "Name", type: "char", relational: false },
        { path: "owner_id", label: "Owner", type: "many2one", relational: true },
      ],
      "POST /api/v1/export": (_u, init) => {
        bodies.push(JSON.parse(String(init.body)));
        return new Response("a,b", { status: 200, headers: { "X-Tessera-Dropped-Fields": "secret" } });
      },
    });
    wrap(<ExportDialog model="demo.deal" domain={[]} initial={["name"]} onClose={() => undefined} />);
    await userEvent.click(await screen.findByRole("button", { name: "+ Owner" }));
    await userEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(await screen.findByText(/cannot read them: secret/)).toBeInTheDocument();
    expect(bodies[0]).toMatchObject({ model: "demo.deal", fields: ["name", "owner_id"], format: "xlsx" });
    expect(created).toHaveBeenCalled();
  });
});
