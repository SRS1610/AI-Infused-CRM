import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { mockFetch } from "../../test/mockApi";
import type { ViewsResponse } from "../api/types";
import { ListView } from "./ListView";
import { emptySearch, SearchBar, type SearchState } from "./SearchBar";
import { buildDomain } from "./searchDomain";

const meta: ViewsResponse = {
  model: {
    name: "demo.item",
    description: "Demo item",
    rec_name: "name",
    order: "name",
    methods: [],
    access: { read: true, write: true, create: true, unlink: true },
  },
  fields: {
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
    qty: { name: "qty", type: "integer", string: "Quantity", required: false, readonly: false, store: true },
    active: { name: "active", type: "boolean", string: "Active", required: false, readonly: false, store: true },
    partner_id: {
      name: "partner_id",
      type: "many2one",
      string: "Partner",
      required: false,
      readonly: false,
      store: true,
      comodel: "partner",
    },
  },
  views: {
    list: {
      type: "list",
      model: "demo.item",
      columns: [{ field: "name" }, { field: "state" }, { field: "qty" }, { field: "active", optional: "hide" }],
    },
    search: { type: "search", model: "demo.item", fields: ["name"], filters: [], group_bys: [{ field: "state" }] },
  },
};

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {ui}
    </QueryClientProvider>,
  );

describe("list view extras", () => {
  beforeEach(() => window.localStorage.clear());

  it("groups rows, expands a group and shows counts", async () => {
    mockFetch({
      "POST /api/v1/models/demo.item/read_group": (_url, init) => {
        const body = JSON.parse(String(init.body));
        if (!body.groupby.length) return { groups: [{ __count: 3, __domain: [], "qty:sum": 6 }] };
        return {
          groups: [
            { __count: 2, __domain: [["state", "=", "draft"]], state: "draft" },
            { __count: 1, __domain: [["state", "=", "done"]], state: "done" },
          ],
        };
      },
      "GET /api/v1/models/demo.item": () => ({
        items: [
          { id: 1, name: "One", state: "draft", qty: 1 },
          { id: 2, name: "Two", state: "draft", qty: 2 },
        ],
        total: null,
        limit: 200,
        offset: 0,
      }),
    });
    wrap(
      <ListView
        model="demo.item"
        meta={meta}
        view={meta.views.list}
        domain={[]}
        groupBy={["state"]}
        onOpen={vi.fn()}
      />,
    );
    const draft = await screen.findByRole("button", { name: /Draft \(2\)/ });
    expect(screen.getByText("2 groups")).toBeInTheDocument();
    await userEvent.click(draft);
    expect(await screen.findByText("Two")).toBeInTheDocument();
    expect(draft).toHaveAttribute("aria-expanded", "true");
  });

  it("selects rows for bulk archive/delete and toggles optional columns", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const { calls } = mockFetch({
      "GET /api/v1/models/demo.item": () => ({
        items: [
          { id: 1, name: "One", display_name: "One", state: "draft", qty: 1, active: true },
          { id: 2, name: "Two", state: "done", qty: 2, active: true },
        ],
        total: 2,
        limit: 80,
        offset: 0,
      }),
      "POST /api/v1/models/demo.item/read_group": () => ({ groups: [{ __count: 2, __domain: [], "qty:sum": 3 }] }),
      "POST /api/v1/models/demo.item/1/call/action_archive": () => ({ result: true }),
      "POST /api/v1/models/demo.item/2/call/action_archive": () => ({ result: true }),
      "DELETE /api/v1/models/demo.item/1": () => undefined,
      "DELETE /api/v1/models/demo.item/2": () => undefined,
    });
    wrap(<ListView model="demo.item" meta={meta} view={meta.views.list} domain={[]} groupBy={[]} onOpen={vi.fn()} />);
    expect(await screen.findByText("One")).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /Active/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Columns" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Active" }));
    expect(screen.getByRole("columnheader", { name: /Active/ })).toBeInTheDocument();
    expect(JSON.parse(window.localStorage.getItem("tessera.columns.demo.item")!)).toEqual([]);
    await userEvent.click(screen.getByRole("checkbox", { name: "Select all" }));
    const bulk = screen.getByRole("toolbar", { name: "Bulk actions" });
    expect(within(bulk).getByText("2 selected")).toBeInTheDocument();
    await userEvent.click(within(bulk).getByRole("button", { name: "Archive" }));
    await waitFor(() => expect(calls.filter((c) => c.path.endsWith("/call/action_archive"))).toHaveLength(2));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select One" }));
    await userEvent.click(
      within(screen.getByRole("toolbar", { name: "Bulk actions" })).getByRole("button", { name: "Delete" }),
    );
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.path.endsWith("/demo.item/1"))).toBe(true));
    confirm.mockRestore();
  });

  it("paginates large result sets", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/models/demo.item": (url) => ({
        items: [{ id: Number(url.searchParams.get("offset")) + 1, name: `Row ${url.searchParams.get("offset")}` }],
        total: 200,
        limit: 80,
        offset: Number(url.searchParams.get("offset")),
      }),
      "POST /api/v1/models/demo.item/read_group": () => ({ groups: [{ __count: 200, __domain: [], "qty:sum": 0 }] }),
    });
    wrap(<ListView model="demo.item" meta={meta} view={meta.views.list} domain={[]} groupBy={[]} onOpen={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Next page" }));
    expect(await screen.findByText("81–160 / 200")).toBeInTheDocument();
    expect(calls.some((c) => c.url.searchParams.get("offset") === "80")).toBe(true);
  });
});

describe("search panel", () => {
  function Harness({ onChange }: { onChange: (s: SearchState) => void }) {
    const [value, setValue] = useState<SearchState>(emptySearch);
    return (
      <SearchBar
        model="demo.item"
        meta={meta}
        view={meta.views.search}
        value={value}
        onChange={(v) => {
          setValue(v);
          onChange(v);
        }}
      />
    );
  }

  it("builds custom conditions, group-bys and saves favourites", async () => {
    const prompt = vi.spyOn(window, "prompt").mockReturnValue("My big ones");
    const { calls } = mockFetch({
      "GET /api/v1/models/web.filter": () => ({
        items: [{ id: 4, name: "Saved", domain: [["state", "=", "done"]], group_bys: ["state"] }],
        total: 1,
        limit: 80,
        offset: 0,
      }),
      "POST /api/v1/models/web.filter": () => ({ id: 5 }),
    });
    const onChange = vi.fn();
    wrap(<Harness onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: "Filters" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Field" }), "qty");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Operator" }), ">");
    await userEvent.type(screen.getByRole("spinbutton", { name: "Value" }), "5");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(onChange.mock.lastCall![0].custom).toEqual([["qty", ">", 5]]);
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Field" }), "partner_id");
    await userEvent.type(screen.getByRole("textbox", { name: "Value" }), "acme");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(onChange.mock.lastCall![0].custom[1]).toEqual(["partner_id.display_name", "ilike", "acme"]);
    await userEvent.click(screen.getByRole("checkbox", { name: "State" }));
    expect(onChange.mock.lastCall![0].groupBy).toEqual(["state"]);
    await userEvent.click(screen.getByRole("button", { name: "Save current search" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")!.body.values.name).toBe("My big ones"));
    expect(calls.find((c) => c.method === "POST")!.body.values.domain).toEqual({
      all: [
        ["qty", ">", 5],
        ["partner_id.display_name", "ilike", "acme"],
      ],
    });
    await userEvent.click(await screen.findByRole("button", { name: "★ Saved" }));
    expect(onChange.mock.lastCall![0]).toEqual({
      text: "",
      filters: [],
      groupBy: ["state"],
      custom: [[["state", "=", "done"]]],
    });
    await userEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(onChange.mock.lastCall![0].custom).toEqual([]);
    prompt.mockRestore();
  });

  it("combines text, filters and custom conditions", () => {
    const view = {
      ...meta.views.search,
      filters: [
        { name: "d", label: "Done", domain: [["state", "=", "done"]] },
        { name: "x", label: "Big", domain: [["qty", ">", 9]] },
      ],
    };
    expect(
      buildDomain(meta, view, { text: "a", filters: ["d", "x"], groupBy: [], custom: [["active", "=", true]] }),
    ).toEqual({
      all: [
        ["name", "ilike", "a"],
        {
          any: [
            ["state", "=", "done"],
            ["qty", ">", 9],
          ],
        },
        ["active", "=", true],
      ],
    });
  });
});
