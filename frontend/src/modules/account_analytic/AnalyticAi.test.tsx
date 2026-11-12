import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { formPanelsFor } from "../../kernel/extensions";
import { ALLOCATION } from "./AnalyticAi";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const info = {
  title: "Analytic allocation suggestions",
  task_class: "classify",
  interactive: true,
  writes: true,
  outbound: false,
  disclosure: "",
  risk_class: "minimal",
};
const status = (on = true, available = true) => ({ available, features: on ? { [ALLOCATION]: info } : {} });

function panel(record: Record<string, unknown> = { move_type: "in_invoice" }) {
  const p = formPanelsFor("account.move").find((x) => x.title === "Analytic suggestions (AI)");
  expect(p).toBeDefined();
  return wrap(p!.render({ model: "account.move", resId: 12, record, reload: () => undefined }));
}

const proposal = {
  id: 77,
  kind: "write",
  title: "Analytic distribution for 'Server hosting October'",
  res_model: "account.move_line",
  res_id: 120,
  payload: { analytic_distribution: { "5": 100 } },
  original: { analytic_distribution: null },
  state: "pending",
  reason: null,
  ai_generated: true,
  run_id: 4,
  feature: ALLOCATION,
};

describe("Analytic suggestions on an invoice or bill", () => {
  it("shows each line's suggestion with its source and the proposal to accept", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "POST /api/v1/account_analytic/ai/moves/12/suggest": () => ({
        rows: [
          {
            line_id: 120,
            label: "Server hosting October",
            suggestion: { "5": 100 },
            accounts: [{ id: 5, name: "[EV-ops] Operations", plan: "Departments", percent: 100 }],
            source: "history",
            proposal,
          },
          { line_id: 121, label: "Rent", suggestion: {}, accounts: [], source: null, proposal: null },
          {
            line_id: 122,
            label: "Laptops",
            accounts: [{ id: 6, name: "Admin", plan: "Departments", percent: 50 }],
            source: "models",
            proposal: null,
          },
        ],
      }),
      "POST /api/v1/ai/proposals/77/accept": () => ({ ...proposal, state: "accepted", ai_generated: false }),
    });
    panel();
    await userEvent.click(await screen.findByRole("button", { name: "Suggest analytic distributions" }));
    expect(await screen.findByText(/\[EV-ops\] Operations 100%/)).toBeInTheDocument();
    expect(screen.getByText("(from similar earlier lines)")).toBeInTheDocument();
    expect(screen.getByText("No similar line to learn from yet.")).toBeInTheDocument();
    expect(screen.getByText(/Already allocated this way/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/ai/proposals/77/accept")).toBe(true));
  });

  it("is hidden on journal entries and when the feature is off; shows errors", async () => {
    mockFetch({ "GET /api/v1/ai/status": () => status() });
    const entry = panel({ move_type: "entry" });
    expect(entry.container.querySelector(".ai-panel-hidden")).not.toBeNull();
    entry.unmount();
    mockFetch({ "GET /api/v1/ai/status": () => status(false) });
    const off = panel();
    await waitFor(() => expect(off.container.querySelector(".ai-panel-hidden")).not.toBeNull());
    off.unmount();
    mockFetch({
      "GET /api/v1/ai/status": () => status(true, false),
      "POST /api/v1/account_analytic/ai/moves/12/suggest": () =>
        new Response(JSON.stringify({ code: "feature_disabled", message: "off" }), { status: 404 }),
    });
    panel({ move_type: "out_invoice" });
    expect(await screen.findByText("AI unavailable")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Suggest analytic distributions" }));
    expect(await screen.findByText("This AI feature is turned off.")).toBeInTheDocument();
  });
});
