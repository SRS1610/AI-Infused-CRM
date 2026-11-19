import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { formPanelsFor } from "../../kernel/extensions";
import { VARIANCE } from "./BudgetAi";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const info = {
  title: "Plain-language variance explanations",
  task_class: "summarize",
  interactive: true,
  writes: false,
  outbound: false,
  disclosure: "",
  risk_class: "minimal",
};
const status = (on = true, available = true) => ({ available, features: on ? { [VARIANCE]: info } : {} });

function panel() {
  const p = formPanelsFor("account.budget").find((x) => x.title === "Explain variance (AI)");
  expect(p).toBeDefined();
  return wrap(p!.render({ model: "account.budget", resId: 3, record: {}, reload: () => undefined }));
}

describe("Budget variance explanation", () => {
  it("explains the variances with a focus, the AI label and removed figures", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "POST /api/v1/ai/features/account_budget.variance_explain/run": () => ({
        run_id: 6,
        feature: VARIANCE,
        text: "Travel is 2,400.00 USD over plan.",
        data: { explanation: "Travel is 2,400.00 USD over plan.", removed_numbers: ["30%"], as_of: "2026-06-30" },
        ai_generated: true,
        citations: [],
        flags: [],
        proposals: [],
        status: "ok",
      }),
      "POST /api/v1/ai/runs/6/feedback": () => ({ id: 1 }),
    });
    panel();
    await userEvent.type(await screen.findByLabelText(/Focus/), "travel");
    await userEvent.click(screen.getByRole("button", { name: "Explain variance" }));
    expect(await screen.findByText("Travel is 2,400.00 USD over plan.")).toBeInTheDocument();
    expect(screen.getByText("✦ AI explanation")).toBeInTheDocument();
    expect(screen.getByText("as of 2026-06-30")).toBeInTheDocument();
    expect(screen.getByText(/were removed: 30%/)).toBeInTheDocument();
    expect(calls.find((c) => c.path.endsWith("variance_explain/run"))?.body).toEqual({
      res_model: "account.budget",
      res_id: 3,
      inputs: { focus: "travel" },
    });
    await userEvent.click(screen.getByTitle("Helpful"));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/ai/runs/6/feedback")).toBe(true));
  });

  it("is hidden when off, unavailable without a gateway, and shows errors", async () => {
    mockFetch({ "GET /api/v1/ai/status": () => status(false) });
    const off = panel();
    await waitFor(() => expect(off.container.querySelector(".ai-panel-hidden")).not.toBeNull());
    off.unmount();
    mockFetch({ "GET /api/v1/ai/status": () => status(true, false) });
    const down = panel();
    expect(await screen.findByText("AI unavailable")).toBeInTheDocument();
    down.unmount();
    mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "POST /api/v1/ai/features/account_budget.variance_explain/run": () =>
        new Response(JSON.stringify({ code: "budget_exceeded", message: "no" }), { status: 429 }),
    });
    panel();
    await userEvent.click(await screen.findByRole("button", { name: "Explain variance" }));
    expect(await screen.findByText("The AI budget for this period is used up.")).toBeInTheDocument();
  });
});
