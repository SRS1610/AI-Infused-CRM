import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { leadFields } from "../../test/studioFixtures";
import type { Domain, ViewsResponse } from "../api/types";
import { conditionOperatorsFor, operatorsFor } from "../conditions";
import { evaluate } from "../domain";
import { SessionProvider } from "../session";
import { buildCondition, ConditionEditor, ConditionSummary, parseCondition } from "./ConditionEditor";
import { FormView } from "./FormView";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SessionProvider>
        <MemoryRouter>{ui}</MemoryRouter>
      </SessionProvider>
    </QueryClientProvider>,
  );
}

describe("conditions", () => {
  it("parses simple domains into rows and builds them back", () => {
    expect(parseCondition(null)).toEqual({ rows: [], mode: "all" });
    expect(parseCondition(["stage_id", "=", 4])).toEqual({
      rows: [{ field: "stage_id", op: "=", value: 4 }],
      mode: "all",
    });
    const any = {
      any: [
        ["priority", "=", "1"],
        ["email_from", "is_set"],
      ],
    };
    expect(parseCondition(any)).toEqual({
      rows: [
        { field: "priority", op: "=", value: "1" },
        { field: "email_from", op: "is_set", value: undefined },
      ],
      mode: "any",
    });
    expect(
      parseCondition([
        ["a", "=", 1],
        ["b", "=", 2],
      ])?.rows,
    ).toHaveLength(2);
    expect(parseCondition({ not: ["a", "=", 1] })).toBeNull();
    expect(buildCondition(parseCondition(any)!.rows, "any")).toEqual(any);
    expect(buildCondition([{ field: "a", op: "=", value: "" }], "all")).toBeNull();
    expect(
      buildCondition(
        [
          { field: "a", op: "is_set", value: "" },
          { field: "b", op: ">", value: 2 },
        ],
        "all",
      ),
    ).toEqual({
      all: [
        ["a", "is_set"],
        ["b", ">", 2],
      ],
    });
  });

  it("offers operators by field type", () => {
    expect(conditionOperatorsFor(leadFields.stage_id).map(([op]) => op)).toEqual(["=", "!=", "is_set", "is_not_set"]);
    expect(operatorsFor(leadFields.stage_id).map(([op]) => op)).toContain("ilike");
    expect(conditionOperatorsFor({ ...leadFields.stage_id, type: "many2many" }).map(([op]) => op)).toEqual([
      "is_set",
      "is_not_set",
    ]);
    expect(conditionOperatorsFor(leadFields.priority).map(([op]) => op)).toContain("is_not_set");
    expect(operatorsFor(leadFields.date_deadline)[0][0]).toBe("=");
    expect(operatorsFor(undefined)[0][0]).toBe("ilike");
  });

  it("evaluates contains / does not contain", () => {
    expect(evaluate(["name", "ilike", "acme"], { name: "ACME Corp" })).toBe(true);
    expect(evaluate(["name", "ilike", "acme"], { name: null })).toBe(false);
    expect(evaluate(["name", "not_ilike", "acme"], { name: "Other" })).toBe(true);
    expect(evaluate(["stage_id.display_name", "ilike", "won"], { stage_id: { id: 4, display_name: "Won" } })).toBe(
      true,
    );
  });

  it("edits rows, switches all/any and clears an unsupported domain", async () => {
    mockFetch({
      "GET /api/v1/models/crm.stage/name_search": () => [
        { id: 1, display_name: "New" },
        { id: 4, display_name: "Won" },
      ],
    });
    const changes: (Domain | null)[] = [];
    const { unmount } = wrap(
      <ConditionEditor
        fields={leadFields}
        value={["stage_id", "=", 4]}
        onChange={(d) => changes.push(d)}
        label="When"
      />,
    );
    const group = screen.getByRole("group", { name: "When" });
    await waitFor(() => expect(within(group).getByLabelText("Value")).toHaveValue("4"));
    await userEvent.click(within(group).getByRole("button", { name: "Add a condition" }));
    const fieldSelects = within(group).getAllByLabelText("Field");
    await userEvent.selectOptions(fieldSelects[1], "date_deadline");
    await userEvent.type(within(group).getAllByLabelText("Value")[1], "2026-12-31");
    await userEvent.selectOptions(within(group).getByLabelText("Combine conditions"), "any");
    expect(changes.at(-1)).toEqual({
      any: [
        ["stage_id", "=", 4],
        ["date_deadline", "=", "2026-12-31"],
      ],
    });
    await userEvent.selectOptions(within(group).getAllByLabelText("Operator")[0], "is_set");
    expect(within(group).getAllByLabelText("Value")).toHaveLength(1);
    await userEvent.click(within(group).getAllByRole("button", { name: "Remove condition" })[1]);
    expect(changes.at(-1)).toEqual(["stage_id", "is_set"]);
    unmount();

    const cleared: (Domain | null)[] = [];
    wrap(
      <ConditionEditor
        fields={leadFields}
        value={{ not: ["a", "=", 1] }}
        onChange={(d) => cleared.push(d)}
        label="Raw"
      />,
    );
    expect(screen.getByText('{"not":["a","=",1]}')).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear condition" }));
    expect(cleared).toEqual([null]);
    expect(screen.getByRole("button", { name: "Add a condition" })).toBeInTheDocument();
  });

  it("summarises conditions in plain words", async () => {
    mockFetch({
      "GET /api/v1/models/crm.stage/name_search": () => [{ id: 4, display_name: "Won" }],
      "GET /api/v1/models/contacts.partner/name_search": () => [{ id: 9, display_name: "Acme" }],
    });
    wrap(
      <>
        <ConditionSummary
          fields={leadFields}
          domain={{
            any: [
              ["stage_id", "=", 4],
              ["partner_id", "=", 9],
              ["priority", "=", "1"],
              ["is_won", "=", false],
              ["email_from", "is_set"],
            ],
          }}
        />
        <ConditionSummary fields={leadFields} domain={null} />
        <ConditionSummary fields={leadFields} domain={{ not: ["x", "=", 1] }} />
        <ConditionSummary fields={leadFields} domain={["unknown", "=", 3]} />
      </>,
    );
    expect(
      await screen.findByText("Stage is Won or Customer is Acme or Priority is High or Is won is No or Email is set"),
    ).toBeInTheDocument();
    expect(screen.getByText("always")).toBeInTheDocument();
    expect(screen.getByText('{"not":["x","=",1]}')).toBeInTheDocument();
    expect(screen.getByText("unknown is 3")).toBeInTheDocument();
  });
});

describe("form conditions from field metadata", () => {
  const meta: ViewsResponse = {
    model: {
      name: "crm.lead",
      description: "Lead",
      rec_name: "name",
      order: "id",
      methods: [],
      access: { read: true, write: true, create: true, unlink: true },
    },
    fields: {
      name: { name: "name", type: "char", string: "Name", required: true, readonly: false, store: true },
      priority: leadFields.priority,
      x_reason: {
        name: "x_reason",
        type: "char",
        string: "Reason",
        required: false,
        readonly: false,
        store: true,
        required_if: ["priority", "=", "1"],
      },
      x_locked: {
        name: "x_locked",
        type: "char",
        string: "Locked note",
        required: false,
        readonly: false,
        store: true,
        readonly_if: ["priority", "=", "1"],
      },
    },
    views: {},
  };
  const view = {
    type: "form",
    model: "crm.lead",
    root: {
      kind: "sheet",
      children: [
        { kind: "field", name: "name" },
        { kind: "field", name: "priority" },
        { kind: "field", name: "x_reason" },
        { kind: "field", name: "x_locked" },
      ],
    },
  };

  it("requires and locks fields while the record matches their condition", async () => {
    mockFetch({
      "GET /api/v1/auth/session": () => ({
        user: { id: 2, login: "admin", name: "Admin", lang: "en_US", tz: "UTC" },
        allowed_companies: [],
        selected_companies: [],
        groups: [],
        csrf_token: "c",
        lang: "en_US",
        modules: [],
      }),
      "POST /api/v1/models/crm.lead": () => ({ id: 3 }),
    });
    const onSaved = vi.fn();
    wrap(
      <FormView
        model="crm.lead"
        recordId={null}
        meta={meta}
        view={view}
        defaults={{ name: "Deal", priority: "0" }}
        onSaved={onSaved}
        onBack={vi.fn()}
      />,
    );
    expect(screen.getByLabelText("Reason")).not.toHaveAttribute("aria-required");
    expect(screen.getByLabelText("Locked note")).toBeEnabled();
    await userEvent.selectOptions(screen.getByLabelText(/^Priority/), "1");
    expect(screen.getByLabelText(/^Reason/)).toHaveAttribute("aria-required", "true");
    expect(screen.getByText("Locked note").parentElement!.querySelector(".readonly")).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Reason is required")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/^Reason/), "Budget");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(3));
  });
});
