import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { clientAction } from "../../kernel/extensions";
import { studioRoutes } from "../../test/studioFixtures";
import { moveAfter, reorderOps, slug, step } from "./api";
import "./StudioApp";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

async function views(layout?: string) {
  wrap(clientAction("studio")!());
  await userEvent.click(await screen.findByRole("tab", { name: "Views" }));
  if (layout) await userEvent.click(screen.getByRole("radio", { name: layout }));
}

const opsOf = (calls: { path: string; body: any }[], suffix: string) =>
  calls.find((c) => c.path.endsWith(suffix))!.body.operations;

describe("reorder helpers", () => {
  const anchor = (k: string) => ({ field: k });
  const node = (k: string) => ({ field: k });
  it("produces no operation when nothing moved", () => {
    expect(reorderOps(["a", "b", "c"], ["a", "b", "c"], anchor, node)).toEqual([]);
  });
  it("moves an item to the front with insert_before and others with insert_after", () => {
    expect(reorderOps(["a", "b", "c"], ["c", "a", "b"], anchor, node)).toEqual([
      { op: "remove", anchor: { field: "c" } },
      { op: "insert_before", anchor: { field: "a" }, node: { field: "c" } },
    ]);
    expect(reorderOps(["a", "b", "c"], ["b", "c", "a"], anchor, node)).toEqual([
      { op: "remove", anchor: { field: "b" } },
      { op: "insert_before", anchor: { field: "a" }, node: { field: "b" } },
      { op: "remove", anchor: { field: "c" } },
      { op: "insert_after", anchor: { field: "b" }, node: { field: "c" } },
    ]);
  });
  it("steps and drags", () => {
    expect(step([1, 2, 3], 0, -1)).toEqual([1, 2, 3]);
    expect(step([1, 2, 3], 2, -1)).toEqual([1, 3, 2]);
    expect(moveAfter([1, 2, 3], 0, 2)).toEqual([2, 3, 1]);
    expect(moveAfter([1, 2, 3], 2, 0)).toEqual([1, 3, 2]);
    expect(moveAfter([1, 2, 3], 1, 1)).toEqual([1, 2, 3]);
    expect(slug("Échéance client")).toBe("x_echeance_client");
  });
});

describe("Studio view editors", () => {
  it("edits list columns: order, visibility, optional and a new column", async () => {
    const { calls } = studioRoutes();
    await views("List");
    const editor = await screen.findByRole("region", { name: "List columns" });
    await userEvent.click(await within(editor).findByRole("button", { name: "Move Stage up" }));
    await userEvent.selectOptions(within(editor).getByLabelText("Visibility of Customer"), "");
    await userEvent.selectOptions(within(editor).getByLabelText("Visibility of Name"), "show");
    await userEvent.selectOptions(within(editor).getByLabelText("Column to add"), "email_from");
    await userEvent.click(within(editor).getByRole("button", { name: "Add column" }));
    expect(within(editor).getByText(/\+ Email/)).toBeInTheDocument();
    await userEvent.click(within(editor).getByRole("button", { name: "Save layout" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/list/operations"))).toBe(true));
    expect(opsOf(calls, "/list/operations")).toEqual([
      { op: "remove", anchor: { field: "stage_id" } },
      { op: "insert_after", anchor: { field: "name" }, node: { field: "stage_id" } },
      { op: "set_attr", anchor: { field: "partner_id" }, attrs: { optional: null } },
      { op: "set_attr", anchor: { field: "name" }, attrs: { optional: "show" } },
      { op: "append_to", list: "columns", node: { field: "email_from" } },
    ]);
    expect(calls.find((c) => c.path.endsWith("/list/operations"))!.body.summary).toBe("List columns changed in Studio");
  });

  it("refuses to save an unchanged layout and drags a card field", async () => {
    const { calls } = studioRoutes();
    await views("Kanban");
    const editor = await screen.findByRole("region", { name: "Kanban card" });
    await within(editor).findByRole("button", { name: "Move Priority up" });
    await userEvent.click(within(editor).getByRole("button", { name: "Save layout" }));
    expect(within(editor).getByRole("alert")).toHaveTextContent("Nothing to save");
    const items = within(editor).getAllByRole("listitem");
    fireEvent.dragStart(items[0]);
    fireEvent.dragOver(items[2]);
    fireEvent.drop(items[2]); // Name after Priority
    await userEvent.click(within(items[1]).getByLabelText(/Hide/)); // Customer
    await userEvent.selectOptions(within(editor).getByLabelText("Field to add to the card"), "date_deadline");
    await userEvent.click(within(editor).getByRole("button", { name: "Add to the card" }));
    await userEvent.selectOptions(within(editor).getByLabelText("Field to add to the card"), "stage_id");
    await userEvent.click(within(editor).getByRole("button", { name: "Add to the card" }));
    await userEvent.click(within(editor).getByRole("button", { name: "Do not add Stage" }));
    await userEvent.click(within(editor).getByRole("button", { name: "Save layout" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/kanban/operations"))).toBe(true));
    expect(opsOf(calls, "/kanban/operations")).toEqual([
      { op: "remove", anchor: { field: "partner_id" } },
      { op: "remove", anchor: { field: "priority" } },
      { op: "insert_before", anchor: { field: "name" }, node: { kind: "field", name: "priority" } },
      { op: "append", anchor: { path: "/root" }, node: { kind: "field", name: "date_deadline" } },
    ]);
  });

  it("creates a view from the generated default", async () => {
    const { calls } = studioRoutes();
    await views();
    const form = await screen.findByRole("form", { name: "Create a view" });
    await userEvent.selectOptions(within(form).getByLabelText("View type"), "kanban");
    await userEvent.type(within(form).getByLabelText("View name"), "Compact cards");
    await userEvent.click(within(form).getByLabelText(/Use it as the default view/));
    await userEvent.click(within(form).getByRole("button", { name: "Create view" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/v1/studio/views")).toBe(true));
    expect(calls.find((c) => c.path === "/api/v1/studio/views")!.body).toEqual({
      model: "crm.lead",
      view_type: "kanban",
      name: "Compact cards",
      make_default: false,
    });
  });

  it("shows a server error when a view change is refused", async () => {
    studioRoutes({
      "POST /api/v1/studio/views/crm.lead/form/operations": () =>
        new Response(JSON.stringify({ error: "ValidationError", message: "View anchor not found" }), { status: 422 }),
    });
    await views();
    const editor = await screen.findByRole("region", { name: "Form layout" });
    await userEvent.click(await within(editor).findByRole("button", { name: "Move Customer down" }));
    await userEvent.click(within(editor).getByRole("button", { name: "Save layout" }));
    expect(await within(editor).findByRole("alert")).toHaveTextContent("View anchor not found");
  });
});

describe("Studio search filters", () => {
  it("adds, edits, removes and reorders filters and group-bys", async () => {
    const { calls } = studioRoutes();
    wrap(clientAction("studio")!());
    await userEvent.click(await screen.findByRole("tab", { name: "Search" }));
    const editor = await screen.findByRole("region", { name: "Search filters" });
    const filters = within(editor).getByRole("list", { name: "Filters" });
    await waitFor(() => expect(within(filters).getByText("Stage is Won")).toBeInTheDocument());
    // reorder and edit "Won", remove "My pipeline"
    await userEvent.click(within(filters).getByRole("button", { name: "Move Won up" }));
    await userEvent.click(within(filters).getByRole("button", { name: "Edit filter Won" }));
    const edit = within(editor).getByRole("group", { name: "Edit filter" });
    await userEvent.clear(within(edit).getByLabelText("Filter name"));
    await userEvent.type(within(edit).getByLabelText("Filter name"), "Won deals");
    await userEvent.click(within(edit).getByRole("button", { name: "Apply" }));
    await userEvent.click(within(filters).getByRole("button", { name: "Remove filter My pipeline" }));
    // a new filter
    const add = within(editor).getByRole("group", { name: "New filter" });
    await userEvent.type(within(add).getByLabelText("Filter name"), "Big deals");
    await userEvent.click(within(add).getByRole("button", { name: "Add a condition" }));
    await userEvent.selectOptions(within(add).getByLabelText("Field"), "expected_revenue");
    await userEvent.selectOptions(within(add).getByLabelText("Operator"), ">");
    await userEvent.type(within(add).getByLabelText("Value"), "10000");
    await userEvent.click(within(add).getByRole("button", { name: "Add filter" }));
    expect(within(filters).getByText("Expected revenue > 10000")).toBeInTheDocument();
    // group-bys
    await userEvent.click(within(editor).getByRole("button", { name: "Remove grouping Stage" }));
    await userEvent.selectOptions(within(editor).getByLabelText("Grouping to add"), "priority");
    await userEvent.click(within(editor).getByRole("button", { name: "Add grouping" }));
    await userEvent.click(within(editor).getByRole("button", { name: "Save search filters" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/search/operations"))).toBe(true));
    expect(opsOf(calls, "/search/operations")).toEqual([
      { op: "remove", anchor: { name: "mine" } },
      { op: "set_attr", anchor: { name: "won" }, attrs: { label: "Won deals", domain: [["stage_id", "=", 4]] } },
      {
        op: "append_to",
        list: "filters",
        node: { name: "x_big_deals", label: "Big deals", domain: ["expected_revenue", ">", 10000] },
      },
      { op: "remove", anchor: { field: "stage_id" } },
      { op: "append_to", list: "group_bys", node: { field: "priority" } },
    ]);
  });

  it("refuses to save unchanged filters", async () => {
    studioRoutes();
    wrap(clientAction("studio")!());
    await userEvent.click(await screen.findByRole("tab", { name: "Search" }));
    const editor = await screen.findByRole("region", { name: "Search filters" });
    await userEvent.click(within(editor).getByRole("button", { name: "Save search filters" }));
    expect(within(editor).getByRole("alert")).toHaveTextContent("Nothing to save");
  });
});
