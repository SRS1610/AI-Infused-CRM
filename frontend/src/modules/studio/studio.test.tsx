import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { clientAction } from "../../kernel/extensions";
import "./StudioApp";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const views = {
  model: { name: "crm.lead", description: "Lead", rec_name: "name", order: "id", methods: [], access: {} },
  fields: {},
  views: {
    form: {
      type: "form",
      model: "crm.lead",
      view_id: 3,
      root: {
        kind: "sheet",
        children: [
          { kind: "field", name: "name" },
          {
            kind: "group",
            children: [
              { kind: "field", name: "partner_id" },
              { kind: "field", name: "stage_id" },
            ],
          },
        ],
      },
    },
    list: { type: "list", model: "crm.lead", columns: [{ field: "name" }] },
  },
};

function routes() {
  return mockFetch({
    "GET /api/v1/studio/models": () => [{ model: "crm.lead", description: "Lead", module: "crm" }],
    "GET /api/v1/studio/models/crm.lead/fields": () => [
      { name: "name", label: "Name", type: "char", studio: false },
      { name: "partner_id", label: "Customer", type: "many2one", studio: false },
      { name: "stage_id", label: "Stage", type: "many2one", studio: false },
    ],
    "GET /api/v1/web/views": () => views,
    "GET /api/v1/studio/changes": () => [
      {
        id: 7,
        kind: "add_field",
        model: "crm.lead",
        summary: "Added field Competitor",
        state: "applied",
        user: "Admin",
        date: null,
      },
    ],
    "GET /api/v1/studio/snapshots": () => [{ id: 2, name: "Before demo", date: "2026-11-10T10:00:00Z" }],
    "POST /api/v1/studio/fields": () => ({ field: "x_competitor", change_id: 8 }),
    "POST /api/v1/studio/views/crm.lead/form/operations": () => ({ id: 9 }),
    "POST /api/v1/studio/automations": () => ({ id: 10 }),
    "POST /api/v1/studio/changes/7/rollback": () => ({ id: 7, state: "rolled_back" }),
    "POST /api/v1/studio/snapshots/2/rollback": () => ({ rolled_back: 3 }),
    "POST /api/v1/studio/snapshots": () => ({ id: 3 }),
  });
}

describe("Studio", () => {
  it("adds a field with its placement", async () => {
    const { calls } = routes();
    wrap(clientAction("studio")!());
    const form = await screen.findByRole("form", { name: "Add a field" });
    await userEvent.type(within(form).getByLabelText("Label"), "Competitor offer");
    await userEvent.selectOptions(within(form).getByLabelText("Type"), "monetary");
    await waitFor(() => expect(within(form).getAllByRole("option", { name: "Customer" }).length).toBeGreaterThan(0));
    await userEvent.selectOptions(within(form).getByLabelText("Place on the form after"), "partner_id");
    await userEvent.click(within(form).getByRole("button", { name: "Add field" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/studio/fields")).toBe(true));
    expect(calls.find((c) => c.path === "/api/v1/studio/fields")!.body).toMatchObject({
      model: "crm.lead",
      name: "x_competitor_offer",
      type: "monetary",
      label: "Competitor offer",
      placements: [{ view_type: "form", after: "partner_id" }, { view_type: "list" }],
    });
  });

  it("reorders, locks and hides form fields as view operations", async () => {
    const { calls } = routes();
    wrap(clientAction("studio")!());
    await waitFor(() =>
      expect(within(screen.getByRole("region", { name: "Form layout" })).getByText("stage_id")).toBeInTheDocument(),
    );
    const layout = screen.getByRole("region", { name: "Form layout" });
    await userEvent.click(within(layout).getByRole("button", { name: "Move stage_id up" }));
    const rows = within(layout).getAllByRole("listitem");
    // order is now name, stage_id, partner_id: hide name, lock stage_id
    await userEvent.click(within(rows[1]).getByLabelText(/Read-only/));
    await userEvent.click(within(rows[0]).getByLabelText(/Hide/));
    await userEvent.click(within(layout).getByRole("button", { name: "Save layout" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/form/operations"))).toBe(true));
    const ops = calls.find((c) => c.path.endsWith("/form/operations"))!.body.operations;
    expect(ops).toContainEqual({ op: "remove", anchor: { field: "name" } });
    expect(ops).toContainEqual({ op: "set_attr", anchor: { field: "stage_id" }, attrs: { readonly: true } });
    expect(
      ops.some(
        (o: { op: string; anchor: { field: string } }) => o.op === "insert_after" && o.anchor.field === "stage_id",
      ),
    ).toBe(true);
  });

  it("creates an automation, rolls back changes and snapshots", async () => {
    const { calls } = routes();
    wrap(clientAction("studio")!());
    const auto = await screen.findByRole("form", { name: "Add an automated action" });
    await userEvent.type(within(auto).getByLabelText("Name"), "Thank-you call");
    await userEvent.click(within(auto).getByRole("button", { name: "Add automation" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/studio/automations")).toBe(true));
    expect(calls.find((c) => c.path === "/api/v1/studio/automations")!.body).toMatchObject({
      model_name: "crm.lead",
      trigger: "on_write",
      actions: [{ type: "schedule_activity", summary: '"Follow up"', days: 1 }],
    });
    const history = screen.getByRole("region", { name: "Change history" });
    await userEvent.click(await within(history).findByRole("button", { name: "Roll back" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/studio/changes/7/rollback")).toBe(true));
    await userEvent.click(within(history).getByRole("button", { name: "Roll back to here" }));
    await userEvent.click(within(history).getByRole("button", { name: "Take a snapshot" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/studio/snapshots/2/rollback")).toBe(true));
    expect(within(history).getByRole("link", { name: "Export as module" })).toHaveAttribute(
      "href",
      "/api/v1/studio/export",
    );
  });
});
