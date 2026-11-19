import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import type { ViewsResponse } from "../api/types";
import { SessionProvider } from "../session";
import { ActionView } from "./ActionView";
import { FormView } from "./FormView";
import { KanbanView } from "./KanbanView";
import { ListView } from "./ListView";

// Generated views for a scaffolded module (no module-specific client code) — acceptance 1.2-h.
const meta: ViewsResponse = {
  model: {
    name: "demo.item",
    description: "Demo item",
    rec_name: "name",
    order: "name",
    methods: ["action_done"],
    access: { read: true, write: true, create: true, unlink: true },
  },
  fields: {
    id: { name: "id", type: "integer", string: "ID", required: false, readonly: true, store: true },
    display_name: {
      name: "display_name",
      type: "char",
      string: "Display name",
      required: false,
      readonly: true,
      store: false,
    },
    name: { name: "name", type: "char", string: "Name", required: true, readonly: false, store: true },
    state: {
      name: "state",
      type: "selection",
      string: "State",
      required: true,
      readonly: false,
      store: true,
      selection: [
        ["draft", "Draft"],
        ["done", "Done"],
      ],
    },
    price: {
      name: "price",
      type: "monetary",
      string: "Price",
      required: false,
      readonly: false,
      store: true,
      currency_field: "currency_id",
    },
    currency_id: {
      name: "currency_id",
      type: "many2one",
      string: "Currency",
      required: false,
      readonly: false,
      store: true,
      comodel: "core.currency",
    },
    active: { name: "active", type: "boolean", string: "Active", required: false, readonly: false, store: true },
  },
  views: {
    form: {
      type: "form",
      model: "demo.item",
      generated: true,
      root: {
        kind: "sheet",
        children: [
          { kind: "field", name: "name", widget: "title" },
          {
            kind: "group",
            columns: 2,
            children: [
              { kind: "field", name: "state" },
              { kind: "field", name: "price" },
              { kind: "field", name: "currency_id" },
            ],
          },
          { kind: "buttons", children: [{ kind: "button", method: "action_done", label: "Mark done" }] },
        ],
      },
    },
    list: {
      type: "list",
      model: "demo.item",
      generated: true,
      columns: [{ field: "name" }, { field: "state" }, { field: "price" }],
    },
    kanban: {
      type: "kanban",
      model: "demo.item",
      generated: true,
      root: { kind: "card", children: [{ kind: "field", name: "display_name", widget: "title" }] },
    },
    search: {
      type: "search",
      model: "demo.item",
      generated: true,
      fields: ["name"],
      filters: [{ name: "done", label: "Done", domain: [["state", "=", "done"]] }],
      group_bys: [{ field: "state" }],
    },
  },
};

const rows = [
  {
    id: 1,
    name: "Alpha",
    state: "draft",
    price: 10,
    currency_id: { id: 1, display_name: "USD" },
    display_name: "Alpha",
  },
  {
    id: 2,
    name: "Beta",
    state: "done",
    price: 20.5,
    currency_id: { id: 1, display_name: "USD" },
    display_name: "Beta",
  },
];

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

const baseRoutes = {
  "GET /api/v1/auth/session": () => ({
    user: { id: 2, login: "admin", name: "Admin", lang: "en_US", tz: "UTC" },
    allowed_companies: [],
    selected_companies: [],
    groups: [],
    csrf_token: "c",
    lang: "en_US",
    modules: [],
  }),
  "GET /api/v1/models/web.filter": () => ({ items: [], total: 0, limit: 80, offset: 0 }),
};

describe("generic views", () => {
  it("renders a list with totals, sorting and opens records", async () => {
    const { calls } = mockFetch({
      ...baseRoutes,
      "GET /api/v1/models/demo.item": () => ({ items: rows, total: 2, limit: 80, offset: 0 }),
      "POST /api/v1/models/demo.item/read_group": () => ({ groups: [{ __count: 2, __domain: [], "price:sum": 30.5 }] }),
    });
    const onOpen = vi.fn();
    wrap(<ListView model="demo.item" meta={meta} view={meta.views.list} domain={[]} groupBy={[]} onOpen={onOpen} />);
    expect(await screen.findByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("$20.50")).toBeInTheDocument();
    expect(await screen.findByText("30.5")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^Name/ }));
    await waitFor(() => expect(calls.some((c) => c.url.searchParams.get("order") === "name asc")).toBe(true));
    await userEvent.click(screen.getByText("Beta"));
    expect(onOpen).toHaveBeenCalledWith(2);
  });

  it("inline edits a monetary cell and surfaces optimistic-concurrency conflicts (1.2-i)", async () => {
    let patches = 0;
    mockFetch({
      ...baseRoutes,
      "GET /api/v1/models/demo.item": () => ({ items: rows, total: 2, limit: 80, offset: 0 }),
      "GET /api/v1/models/demo.item/1": () => rows[0],
      "POST /api/v1/models/demo.item/read_group": () => ({ groups: [{ __count: 2, __domain: [], "price:sum": 30.5 }] }),
      "PATCH /api/v1/models/demo.item/1": () => {
        patches += 1;
        return new Response(JSON.stringify({ error: "ConcurrencyError", message: "modified by someone else" }), {
          status: 409,
        });
      },
    });
    wrap(<ListView model="demo.item" meta={meta} view={meta.views.list} domain={[]} groupBy={[]} onOpen={vi.fn()} />);
    fireEvent.doubleClick(await screen.findByText("Alpha"));
    const priceInput = await screen.findByDisplayValue("10");
    await userEvent.clear(priceInput);
    await userEvent.type(priceInput, "12.345");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alertdialog", { name: "Edit conflict" })).toBeInTheDocument();
    expect(patches).toBe(1);
  });

  it("creates through the generated form, validating required fields", async () => {
    const { calls } = mockFetch({
      ...baseRoutes,
      "GET /api/v1/models/core.currency/name_search": () => [{ id: 1, display_name: "USD" }],
      "POST /api/v1/models/demo.item": () => ({ id: 9 }),
    });
    const onSaved = vi.fn();
    wrap(
      <FormView
        model="demo.item"
        recordId={null}
        meta={meta}
        view={meta.views.form}
        defaults={{ state: "draft" }}
        onSaved={onSaved}
        onBack={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Name is required")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/^Name/), "Gamma");
    await userEvent.type(screen.getByLabelText(/^Price/), "3.5");
    const currency = screen.getByRole("combobox", { name: "Currency" });
    await userEvent.type(currency, "US");
    await userEvent.click(await screen.findByRole("option", { name: "USD" }));
    await userEvent.keyboard("{Control>}s{/Control}");
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(9));
    const create = calls.find((c) => c.method === "POST" && c.path === "/api/v1/models/demo.item")!;
    expect(create.body.values).toEqual({ state: "draft", name: "Gamma", price: 3.5, currency_id: 1 });
  });

  it("edits an existing record with its ETag and calls exposed methods", async () => {
    const { calls } = mockFetch({
      ...baseRoutes,
      "GET /api/v1/models/demo.item/1": () => rows[0],
      "PATCH /api/v1/models/demo.item/1": () => ({ id: 1 }),
      "POST /api/v1/models/demo.item/1/call/action_done": () => ({ result: true }),
    });
    wrap(
      <FormView model="demo.item" recordId={1} meta={meta} view={meta.views.form} onSaved={vi.fn()} onBack={vi.fn()} />,
    );
    const title = await screen.findByDisplayValue("Alpha");
    await userEvent.type(title, " 2");
    await userEvent.click(screen.getByRole("button", { name: "Mark done" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/call/action_done"))).toBe(true));
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.headers["If-Match"]).toBe('"etag-1"');
    expect(patch.body.values).toEqual({ name: "Alpha 2" });
  });

  it("moves kanban cards between columns", async () => {
    const { calls } = mockFetch({
      ...baseRoutes,
      "POST /api/v1/models/demo.item/read_group": () => ({
        groups: [
          { __count: 1, __domain: [["state", "=", "draft"]], state: "draft" },
          { __count: 1, __domain: [["state", "=", "done"]], state: "done" },
        ],
      }),
      "GET /api/v1/models/demo.item": (url) => {
        const domain = url.searchParams.get("domain") ?? "";
        return { items: domain.includes("done") ? [rows[1]] : [rows[0]], total: 1, limit: 40, offset: 0 };
      },
      "PATCH /api/v1/models/demo.item/1": () => ({ id: 1 }),
    });
    wrap(
      <KanbanView model="demo.item" meta={meta} view={meta.views.kanban} domain={[]} groupBy={[]} onOpen={vi.fn()} />,
    );
    const done = await screen.findByRole("listitem", { name: "Done" });
    const card = await screen.findByText("Alpha");
    const data = new Map<string, string>();
    const dataTransfer = {
      setData: (k: string, v: string) => data.set(k, v),
      getData: (k: string) => data.get(k) ?? "",
    };
    fireEvent.dragStart(card.closest("li")!, { dataTransfer });
    fireEvent.dragOver(done, { dataTransfer });
    fireEvent.drop(done, { dataTransfer });
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body.values).toEqual({ state: "done" }));
    expect(within(done).getByText("Beta")).toBeInTheDocument();
  });

  it("drives an action end to end: search filters build the domain", async () => {
    const { calls } = mockFetch({
      ...baseRoutes,
      "GET /api/v1/web/actions/demo.action_items": () => ({
        id: 1,
        xmlid: "demo.action_items",
        name: "Items",
        kind: "window",
        model: "demo.item",
        view_types: ["list", "kanban", "form"],
        view_refs: {},
        search_view_ref: null,
        domain: null,
        context: {},
        target: "current",
        url: null,
        client_tag: null,
        limit: 80,
        help: null,
      }),
      "GET /api/v1/web/views": () => meta,
      "GET /api/v1/models/demo.item": () => ({ items: rows, total: 2, limit: 80, offset: 0 }),
      "POST /api/v1/models/demo.item/read_group": () => ({ groups: [{ __count: 2, __domain: [], "price:sum": 30.5 }] }),
    });
    wrap(
      <Routes>
        <Route path="/a/:actionRef" element={<ActionView />} />
      </Routes>,
      "/a/demo.action_items",
    );
    expect(await screen.findByRole("heading", { name: "Items" })).toBeInTheDocument();
    await userEvent.type(screen.getByRole("searchbox"), "alp");
    await userEvent.click(screen.getByRole("button", { name: /Filters/ }));
    await userEvent.click(screen.getByLabelText("Done"));
    await waitFor(() => {
      const domains = calls
        .filter((c) => c.path === "/api/v1/models/demo.item")
        .map((c) => c.url.searchParams.get("domain"));
      expect(domains).toContain(
        JSON.stringify({
          all: [
            ["name", "ilike", "alp"],
            ["state", "=", "done"],
          ],
        }),
      );
    });
  });
});
