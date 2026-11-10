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

async function openWorkflow() {
  wrap(clientAction("studio")!());
  await userEvent.click(await screen.findByRole("tab", { name: "Workflow" }));
  return screen.findByRole("form", { name: "Build a workflow" });
}

const existing = {
  id: 3,
  name: "Approval",
  state_field: "priority",
  strict: true,
  studio_change_id: 20,
  states: [
    { key: "0", name: "Normal", fold: false, done: false },
    { key: "1", name: "High", fold: false, done: true },
  ],
  transitions: [
    {
      id: 1,
      name: "Escalate",
      from: ["0"],
      to: "1",
      condition: [["stage_id", "=", 4]],
      groups: ["crm.group_crm_manager"],
    },
    { id: 2, name: "Reset", from: [], to: "0", condition: null, groups: [] },
  ],
};

describe("Studio workflow builder", () => {
  it("builds a workflow on a new choice field", async () => {
    const { calls } = studioRoutes();
    const form = await openWorkflow();
    await userEvent.type(within(form).getByLabelText("Workflow name"), "Approval");
    await userEvent.clear(within(form).getByLabelText("Label of the new field"));
    await userEvent.type(within(form).getByLabelText("Label of the new field"), "Approval state");
    for (const name of ["Draft", "Submitted", "Approved", "Refused"]) {
      await userEvent.click(within(form).getByRole("button", { name: "Add a state" }));
      const inputs = within(form).getAllByLabelText("State name");
      await userEvent.type(inputs[inputs.length - 1], name);
    }
    const states = within(form).getByRole("list", { name: "States" });
    const rows = within(states).getAllByRole("listitem");
    await userEvent.click(within(rows[2]).getByLabelText(/Done/));
    await userEvent.click(within(rows[3]).getByLabelText(/Folded/));
    await userEvent.click(within(rows[3]).getByLabelText(/Done/));
    await userEvent.click(within(states).getByRole("button", { name: "Move Refused up" }));
    await userEvent.click(within(states).getByRole("button", { name: "Move Refused down" }));

    await userEvent.click(within(form).getByRole("button", { name: "Add a transition" }));
    let tr = within(form).getByRole("group", { name: "Transition …" });
    await userEvent.type(within(tr).getByLabelText("Button label"), "Submit");
    tr = within(form).getByRole("group", { name: "Transition Submit" });
    await userEvent.selectOptions(within(tr).getByLabelText("From"), "Draft");
    await userEvent.selectOptions(within(tr).getByLabelText("To"), "Submitted");
    await userEvent.click(within(tr).getByLabelText(/Main button/));

    await userEvent.click(within(form).getByRole("button", { name: "Add a transition" }));
    tr = within(form).getByRole("group", { name: "Transition …" });
    await userEvent.type(within(tr).getByLabelText("Button label"), "Approve");
    tr = within(form).getByRole("group", { name: "Transition Approve" });
    await userEvent.selectOptions(within(tr).getByLabelText("From"), "Submitted");
    await userEvent.selectOptions(within(tr).getByLabelText("To"), "Approved");
    await waitFor(() => expect(within(tr).getByRole("option", { name: "Sales / CRM / Manager" })).toBeInTheDocument());
    await userEvent.selectOptions(within(tr).getByLabelText("Allowed group"), "crm.group_crm_manager");
    await userEvent.click(within(tr).getByText("Only when…"));
    const condition = within(tr).getByRole("group", { name: "Condition of Approve" });
    await userEvent.click(within(condition).getByRole("button", { name: "Add a condition" }));
    await userEvent.selectOptions(within(condition).getByLabelText("Field"), "is_won");

    await userEvent.click(within(form).getByRole("button", { name: "Add a transition" }));
    tr = within(form).getByRole("group", { name: "Transition …" });
    await userEvent.type(within(tr).getByLabelText("Button label"), "Oops");
    await userEvent.click(within(form).getByRole("button", { name: "Remove transition Oops" }));

    // renaming a state keeps the transitions that use it
    const named = within(form).getAllByLabelText("State name");
    await userEvent.type(named[1], " for review");
    const overview = within(form).getByRole("list", { name: "Workflow overview" });
    expect(within(overview).getByText("Submit to Submitted for review")).toBeInTheDocument();
    expect(within(overview).getByText(/for Sales \/ CRM \/ Manager/)).toBeInTheDocument();

    await userEvent.click(within(form).getByRole("button", { name: "Create workflow" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/studio/workflows")).toBe(true));
    expect(calls.find((c) => c.path === "/api/v1/studio/workflows")!.body).toEqual({
      model: "crm.lead",
      name: "Approval",
      state_field: "",
      new_field: { label: "Approval state" },
      states: [
        { key: "draft", name: "Draft", fold: false, done: false },
        { key: "submitted_for_review", name: "Submitted for review", fold: false, done: false },
        { key: "approved", name: "Approved", fold: false, done: true },
        { key: "refused", name: "Refused", fold: true, done: true },
      ],
      transitions: [
        {
          name: "Submit",
          from: ["draft"],
          to: "submitted_for_review",
          condition: null,
          groups: [],
          highlight: true,
        },
        {
          name: "Approve",
          from: ["submitted_for_review"],
          to: "approved",
          condition: ["is_won", "=", true],
          groups: ["crm.group_crm_manager"],
          highlight: false,
        },
      ],
      strict: true,
      replace: false,
    });
  }, 20_000);

  it("uses an existing field's values as states", async () => {
    const { calls } = studioRoutes();
    const form = await openWorkflow();
    await userEvent.type(within(form).getByLabelText("Workflow name"), "Stages");
    await userEvent.selectOptions(within(form).getByLabelText("State field"), "stage_id");
    const fill = within(form).getByRole("button", { name: "Use the field's values as states" });
    await waitFor(() => expect(fill).toBeEnabled());
    await userEvent.click(fill);
    const states = within(form).getByRole("list", { name: "States" });
    expect(within(states).getAllByRole("listitem")).toHaveLength(2);
    await userEvent.click(within(states).getByRole("button", { name: "Remove state New" }));
    await userEvent.click(within(form).getByRole("button", { name: "Add a state" }));
    const values = within(states).getAllByLabelText("State value");
    await userEvent.selectOptions(values[1], "1");
    await userEvent.click(within(form).getByLabelText(/Change the state only through the transitions/));
    await userEvent.click(within(form).getByRole("button", { name: "Create workflow" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/studio/workflows")).toBe(true));
    expect(calls.find((c) => c.path === "/api/v1/studio/workflows")!.body).toMatchObject({
      state_field: "stage_id",
      new_field: null,
      states: [
        { key: "4", name: "Won" },
        { key: "1", name: "New" },
      ],
      strict: false,
    });
  });

  it("shows the current workflow and replaces it", async () => {
    const { calls } = studioRoutes({
      "GET /api/v1/studio/models/crm.lead/workflow": () => ({ workflow: existing }),
      "POST /api/v1/studio/workflows": () =>
        new Response(JSON.stringify({ error: "ValidationError", message: "Every state needs its own key" }), {
          status: 422,
        }),
    });
    wrap(clientAction("studio")!());
    await userEvent.click(await screen.findByRole("tab", { name: "Workflow" }));
    const section = await screen.findByRole("region", { name: "Workflow" });
    const overview = await within(section).findByRole("list", { name: "Workflow overview" });
    expect(within(section).getByText(/follows the field Priority/)).toBeInTheDocument();
    expect(within(overview).getByText("Escalate to High")).toBeInTheDocument();
    await waitFor(() => expect(within(overview).getByText("Stage is Won")).toBeInTheDocument());
    expect(within(overview).getAllByText("Reset to Normal")).toHaveLength(2); // offered from every state
    await userEvent.click(within(section).getByRole("button", { name: "Change this workflow" }));
    const form = within(section).getByRole("form", { name: "Build a workflow" });
    expect(within(form).getByLabelText("Workflow name")).toHaveValue("Approval");
    await userEvent.click(within(form).getByRole("button", { name: "Replace the workflow" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/studio/workflows")).toBe(true));
    expect(calls.find((c) => c.path === "/api/v1/studio/workflows")!.body).toMatchObject({
      state_field: "priority",
      replace: true,
      transitions: [
        { name: "Escalate", from: ["0"], to: "1", groups: ["crm.group_crm_manager"] },
        { name: "Reset", from: [], to: "0" },
      ],
    });
    expect(await within(form).findByRole("alert")).toHaveTextContent("Every state needs its own key");
    await userEvent.click(within(form).getByRole("button", { name: "Cancel" }));
    expect(within(section).getByRole("button", { name: "Change this workflow" })).toBeInTheDocument();
  });
});
