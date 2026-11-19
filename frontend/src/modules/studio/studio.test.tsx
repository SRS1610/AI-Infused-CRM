import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { clientAction } from "../../kernel/extensions";
import { studioRoutes } from "../../test/studioFixtures";
import "./StudioApp";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const open = async (tab: string) => {
  wrap(clientAction("studio")!());
  await userEvent.click(await screen.findByRole("tab", { name: tab }));
};

describe("Studio", () => {
  it("adds a field with its placement", async () => {
    const { calls } = studioRoutes();
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
      options: { required: false },
    });
  });

  it("adds a choice field with widget, default, a required condition, groups and a kanban placement", async () => {
    const { calls } = studioRoutes();
    wrap(clientAction("studio")!());
    const form = await screen.findByRole("form", { name: "Add a field" });
    await userEvent.type(within(form).getByLabelText("Label"), "Win reason");
    await userEvent.selectOptions(within(form).getByLabelText("Type"), "selection");
    await userEvent.type(within(form).getByLabelText("Options (comma separated)"), "Price, Product fit");
    await userEvent.click(within(form).getByLabelText(/Show on the kanban card/));
    await userEvent.click(within(form).getByText("More options"));
    await userEvent.selectOptions(within(form).getByLabelText("Widget"), "badge");
    await userEvent.selectOptions(within(form).getByLabelText("Default value"), "product_fit");
    await userEvent.selectOptions(within(form).getByLabelText("Required"), "when");
    const condition = within(form).getByRole("group", { name: "Required when" });
    await userEvent.click(within(condition).getByRole("button", { name: "Add a condition" }));
    await userEvent.selectOptions(within(condition).getByLabelText("Field"), "stage_id");
    await waitFor(() => expect(within(condition).getByRole("option", { name: "Won" })).toBeInTheDocument());
    await userEvent.selectOptions(within(condition).getByLabelText("Value"), "4");
    await userEvent.selectOptions(within(form).getByLabelText("Read-only"), "always");
    await waitFor(() =>
      expect(within(form).getByRole("option", { name: "Sales / CRM / Manager" })).toBeInTheDocument(),
    );
    await userEvent.selectOptions(within(form).getByLabelText("Visible to (empty: everyone)"), "crm.group_crm_manager");
    await userEvent.type(within(form).getByLabelText("Help text"), "Why we won");
    await userEvent.click(within(form).getByRole("button", { name: "Add field" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/studio/fields")).toBe(true));
    const body = calls.find((c) => c.path === "/api/v1/studio/fields")!.body;
    expect(body.placements).toContainEqual({ view_type: "kanban" });
    expect(body.options).toEqual({
      widget: "badge",
      default: "product_fit",
      required: false,
      required_if: ["stage_id", "=", 4],
      readonly: true,
      groups: ["crm.group_crm_manager"],
      help: "Why we won",
      selection: [
        ["price", "Price"],
        ["product_fit", "Product fit"],
      ],
    });
  });

  it("edits the options of a Studio field", async () => {
    const { calls } = studioRoutes();
    wrap(clientAction("studio")!());
    const section = await screen.findByRole("region", { name: "Edit a field" });
    await waitFor(() => expect(within(section).getByRole("option", { name: /Channel/ })).toBeInTheDocument());
    await userEvent.selectOptions(within(section).getByLabelText("Studio field"), "x_channel");
    const form = within(section).getByRole("form", { name: "Field options" });
    await userEvent.click(within(form).getByRole("button", { name: "Save field" }));
    expect(within(form).getByRole("alert")).toHaveTextContent("Nothing to save");
    await userEvent.clear(within(form).getByLabelText("Label"));
    await userEvent.type(within(form).getByLabelText("Label"), "Sales channel");
    await userEvent.type(within(form).getByLabelText(/Options \(one per line/), "\nCold call");
    await userEvent.selectOptions(within(form).getByLabelText("Default value"), "web");
    await userEvent.selectOptions(within(form).getByLabelText("Read-only"), "when");
    const condition = within(form).getByRole("group", { name: "Read-only when" });
    await userEvent.click(within(condition).getByRole("button", { name: "Add a condition" }));
    await userEvent.selectOptions(within(condition).getByLabelText("Field"), "is_won");
    await userEvent.click(within(form).getByRole("button", { name: "Save field" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH")!.body.options).toEqual({
      label: "Sales channel",
      default: "web",
      readonly_if: ["is_won", "=", true],
      selection: [
        ["web", "Web"],
        ["fair", "Fair"],
        ["cold_call", "Cold call"],
      ],
    });
  });

  it("reorders, locks and hides form fields as view operations", async () => {
    const { calls } = studioRoutes();
    await open("Views");
    const layout = await screen.findByRole("region", { name: "Form layout" });
    await userEvent.click(await within(layout).findByRole("button", { name: "Move Stage up" }));
    const rows = within(layout).getAllByRole("listitem");
    // order is now name, stage_id, partner_id: hide name, lock stage_id
    await userEvent.click(within(rows[1]).getByLabelText(/Read-only/));
    await userEvent.click(within(rows[0]).getByLabelText(/Hide/));
    await userEvent.click(within(layout).getByRole("button", { name: "Save layout" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/form/operations"))).toBe(true));
    const ops = calls.find((c) => c.path.endsWith("/form/operations"))!.body.operations;
    expect(ops).toEqual([
      { op: "remove", anchor: { field: "name" } },
      { op: "remove", anchor: { field: "stage_id" } },
      { op: "insert_before", anchor: { field: "partner_id" }, node: { kind: "field", name: "stage_id" } },
      { op: "set_attr", anchor: { field: "stage_id" }, attrs: { readonly: true } },
    ]);
  });

  it("creates an automation, rolls back changes and snapshots", async () => {
    const { calls } = studioRoutes();
    await open("Automations");
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

  it("shows API errors and switches tabs with the arrow keys", async () => {
    studioRoutes({
      "POST /api/v1/studio/changes/7/rollback": () =>
        new Response(JSON.stringify({ error: "ValidationError", message: "A later change relies on this one" }), {
          status: 422,
        }),
    });
    wrap(clientAction("studio")!());
    const history = await screen.findByRole("region", { name: "Change history" });
    await userEvent.click(await within(history).findByRole("button", { name: "Roll back" }));
    expect(await within(history).findByRole("alert")).toHaveTextContent("A later change relies on this one");
    await userEvent.click(screen.getByRole("tab", { name: "Fields" }));
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Views" })).toHaveAttribute("aria-selected", "true");
    await userEvent.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Fields" })).toHaveAttribute("aria-selected", "true");
  });
});
