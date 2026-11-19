import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { formPanelsFor } from "../../kernel/extensions";
import { EXPLAIN } from "./CommissionAi";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const status = (on = true, available = true) => ({
  available,
  features: on
    ? {
        [EXPLAIN]: {
          title: "Commission statement explanation",
          task_class: "summarize",
          interactive: true,
          writes: false,
          outbound: false,
          disclosure: "ai_generated",
          risk_class: "limited",
        },
      }
    : {},
});

function panel() {
  const p = formPanelsFor("sale.commission_statement").find((x) => x.title === "Explain this statement (AI)");
  expect(p).toBeDefined();
  return wrap(p!.render({ model: "sale.commission_statement", resId: 4, record: {}, reload: () => undefined }));
}

describe("Commission statement explanation", () => {
  it("explains the statement with the AI label, removed figures and feedback", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "POST /api/v1/ai/features/sale_commission.explain/run": () => ({
        run_id: 12,
        feature: EXPLAIN,
        text: "Your total for the period is 3,960.90 USD.",
        data: {
          explanation: "Your total for the period is 3,960.90 USD.",
          removed_numbers: ["312.47"],
          total: 3960.9,
          currency: "USD",
        },
        ai_generated: true,
        citations: [],
        flags: ["numbers_removed"],
        proposals: [],
        status: "ok",
      }),
      "POST /api/v1/ai/runs/12/feedback": () => ({ id: 1 }),
    });
    panel();
    await userEvent.click(await screen.findByRole("button", { name: "Explain" }));
    expect(await screen.findByText("Your total for the period is 3,960.90 USD.")).toBeInTheDocument();
    expect(screen.getByText("✦ AI explanation")).toBeInTheDocument();
    expect(screen.getByText(/were removed: 312.47/)).toBeInTheDocument();
    const run = calls.find((c) => c.path === "/api/v1/ai/features/sale_commission.explain/run");
    expect(run?.body).toEqual({ res_model: "sale.commission_statement", res_id: 4 });
    expect(screen.getByRole("button", { name: "Explain again" })).toBeInTheDocument();
    await userEvent.click(screen.getByTitle("Not helpful"));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/ai/runs/12/feedback")).toBe(true));
  });

  it("is hidden when the feature is off and says when AI is unavailable", async () => {
    mockFetch({ "GET /api/v1/ai/status": () => status(false) });
    const { container, unmount } = panel();
    await waitFor(() => expect(container.querySelector(".ai-panel-hidden")).not.toBeNull());
    expect(screen.queryByRole("button", { name: "Explain" })).not.toBeInTheDocument();
    unmount();
    mockFetch({ "GET /api/v1/ai/status": () => status(true, false) });
    panel();
    expect(await screen.findByText("AI unavailable")).toBeInTheDocument();
  });

  it("shows why an explanation could not be made", async () => {
    mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "POST /api/v1/ai/features/sale_commission.explain/run": () =>
        new Response(JSON.stringify({ code: "feature_disabled", message: "off" }), { status: 404 }),
    });
    panel();
    await userEvent.click(await screen.findByRole("button", { name: "Explain" }));
    expect(await screen.findByText("This AI feature is turned off.")).toBeInTheDocument();
  });
});
