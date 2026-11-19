import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { clientAction } from "../../kernel/extensions";
import type { ViewsResponse } from "../../kernel/api/types";
import { FieldInput } from "../../kernel/fields/FieldInput";
import { KanbanView } from "../../kernel/views/KanbanView";
import "./CrmReports";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

describe("CRM client", () => {
  it("renders the sales analysis client action", async () => {
    mockFetch({
      "GET /api/v1/crm/reports/forecast": () => [{ month: "2026-12-01", count: 3, pipeline: 1000, weighted: 450.5 }],
      "GET /api/v1/crm/reports/funnel": () => [
        { stage: "New", count: 2, revenue: 100, reached: 5, conversion: 100 },
        { stage: "Won", count: 3, revenue: 900, reached: 3, conversion: 60 },
      ],
      "GET /api/v1/crm/reports/win-rate": (url) => [
        {
          label: url.searchParams.get("group_by") === "team_id" ? "Sales" : "Admin",
          won: 2,
          lost: 1,
          win_rate: 66.67,
          won_revenue: 900,
        },
      ],
      "GET /api/v1/crm/reports/velocity": () => ({
        open_opportunities: 7,
        average_deal: 450,
        win_rate: 66.67,
        average_cycle_days: 30,
        velocity_per_day: 70,
      }),
    });
    const render_ = clientAction("crm.reports");
    expect(render_).toBeDefined();
    wrap(render_!());
    expect(await screen.findByRole("rowheader", { name: "2026-12" })).toBeInTheDocument();
    expect(screen.getByText("450.50")).toBeInTheDocument();
    expect(await screen.findByRole("rowheader", { name: "Admin" })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Group win rate by"), "team_id");
    expect(await screen.findByRole("rowheader", { name: "Sales" })).toBeInTheDocument();
    expect(screen.getByText("Open opportunities")).toBeInTheDocument();
  });

  it("statusbar and priority widgets", async () => {
    mockFetch({
      "GET /api/v1/models/crm.stage": () => ({
        items: [
          { id: 1, display_name: "New" },
          { id: 2, display_name: "Won" },
        ],
        total: 2,
        limit: 30,
        offset: 0,
      }),
    });
    const onStage = vi.fn();
    wrap(
      <FieldInput
        id="s"
        field={{
          name: "stage_id",
          type: "many2one",
          comodel: "crm.stage",
          string: "Stage",
          required: false,
          readonly: false,
          store: true,
        }}
        value={{ id: 1, display_name: "New" }}
        record={{}}
        onChange={onStage}
        readonly={false}
        node={{ widget: "statusbar" }}
      />,
    );
    await userEvent.click(await screen.findByRole("radio", { name: "Won" }));
    expect(onStage).toHaveBeenCalledWith({ id: 2, display_name: "Won" });
    const onPriority = vi.fn();
    wrap(
      <FieldInput
        id="p"
        field={{
          name: "priority",
          type: "selection",
          string: "Priority",
          required: false,
          readonly: false,
          store: true,
          selection: [
            ["0", "Normal"],
            ["1", "Good"],
            ["2", "Very good"],
          ],
        }}
        value="1"
        record={{}}
        onChange={onPriority}
        readonly={false}
        node={{ widget: "priority" }}
      />,
    );
    await userEvent.click(screen.getByRole("radio", { name: "Very good" }));
    expect(onPriority).toHaveBeenCalledWith("2");
  });

  it("kanban shows every stage with revenue sums", async () => {
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
        stage_id: {
          name: "stage_id",
          type: "many2one",
          string: "Stage",
          required: false,
          readonly: false,
          store: true,
          comodel: "crm.stage",
        },
        expected_revenue: {
          name: "expected_revenue",
          type: "monetary",
          string: "Expected revenue",
          required: false,
          readonly: false,
          store: true,
        },
        display_name: {
          name: "display_name",
          type: "char",
          string: "Name",
          required: false,
          readonly: true,
          store: false,
        },
      },
      views: {},
    };
    const { calls } = mockFetch({
      "POST /api/v1/models/crm.lead/read_group": () => ({
        groups: [
          {
            __count: 1,
            __domain: [["stage_id", "=", 1]],
            stage_id: { id: 1, display_name: "New" },
            "expected_revenue:sum": 12500,
          },
        ],
      }),
      "GET /api/v1/models/crm.stage": () => ({
        items: [
          { id: 1, display_name: "New" },
          { id: 2, display_name: "Won" },
        ],
        total: 2,
        limit: 50,
        offset: 0,
      }),
      "GET /api/v1/models/crm.lead": () => ({ items: [], total: 0, limit: 40, offset: 0 }),
    });
    wrap(
      <KanbanView
        model="crm.lead"
        meta={meta}
        view={{
          type: "kanban",
          model: "crm.lead",
          default_group_by: "stage_id",
          sum_field: "expected_revenue",
          all_groups: true,
          root: { kind: "card", children: [] },
        }}
        domain={[]}
        groupBy={[]}
        onOpen={() => undefined}
      />,
    );
    expect(await screen.findByRole("listitem", { name: "Won" })).toBeInTheDocument();
    const sums = screen.getAllByTitle("Total Expected revenue").map((el) => el.textContent);
    expect(sums).toEqual(["12,500", "0"]);
    await waitFor(() => expect(calls.some((c) => c.body?.aggregates?.[0] === "expected_revenue:sum")).toBe(true));
  });
});
