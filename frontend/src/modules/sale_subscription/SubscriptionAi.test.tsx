import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { formPanelsFor } from "../../kernel/extensions";
import { CHURN, RENEWAL, riskLabel } from "./SubscriptionAi";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const status =
  (names = [CHURN, RENEWAL], available = true) =>
  () => ({
    available,
    features: Object.fromEntries(
      names.map((n) => [
        n,
        {
          title: n,
          task_class: "classify",
          interactive: true,
          writes: false,
          outbound: n === RENEWAL,
          disclosure: "ai_generated",
          risk_class: "limited",
        },
      ]),
    ),
  });

const view = (over: Record<string, unknown> = {}) => ({
  subscription_id: 9,
  is_subscription: true,
  running: true,
  score: 78,
  probability: 0.78,
  risk: "high",
  factors: [
    { feature: "failures", label: "4 failed payments", direction: "raises", weight: 1.9, value: 4 },
    { feature: "log_tenure", label: "Customer for 30 months", direction: "lowers", weight: -0.4, value: 3.4 },
  ],
  scored_at: "2026-11-10T04:29:00Z",
  model: { source: "prior", trained_at: null, rows: null, holdout_auc: null },
  excluded_inputs: "The subscription's state, close reason and the customer's identity are never model inputs",
  ...over,
});

const record = { plan_id: { id: 1, display_name: "Monthly" }, subscription_state: "progress" };

function panel(rec: Record<string, unknown> = record) {
  const p = formPanelsFor("sale.quotation").find((x) => x.title === "Churn and renewal (AI)");
  expect(p).toBeDefined();
  return wrap(p!.render({ model: "sale.quotation", resId: 9, record: rec, reload: () => undefined }));
}

describe("Subscription AI panel", () => {
  it("shows the churn score, its risk and the factors with feedback", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(),
      "GET /api/v1/sale_subscription/ai/subscriptions/9/churn": () => view(),
      "POST /api/v1/sale_subscription/ai/subscriptions/9/churn": () => view({ score: 80, run_id: 55 }),
      "POST /api/v1/ai/runs/55/feedback": () => ({ id: 1 }),
    });
    panel();
    expect(await screen.findByLabelText("Churn score 78 out of 100")).toBeInTheDocument();
    expect(screen.getByText("High churn risk")).toBeInTheDocument();
    expect(screen.getByText("✦ AI score")).toBeInTheDocument();
    expect(screen.getByText("Starting weights until the company has enough history")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Why this score?" }));
    const region = screen.getByRole("region", { name: "Why this score" });
    expect(within(region).getByText("4 failed payments")).toBeInTheDocument();
    expect(within(region).getByText("Raises the risk:")).toBeInTheDocument();
    expect(within(region).getByText("Lowers the risk:")).toBeInTheDocument();
    expect(within(region).getByText(/never model inputs/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Score again" }));
    expect(await screen.findByLabelText("Churn score 80 out of 100")).toBeInTheDocument();
    await userEvent.click(screen.getByTitle("Helpful"));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/ai/runs/55/feedback")).toBe(true));
  });

  it("drafts a renewal e-mail that is only sent through the proposal", async () => {
    const accepted: number[] = [];
    mockFetch({
      "GET /api/v1/ai/status": status([RENEWAL]),
      "POST /api/v1/ai/features/sale_subscription.renewal_draft/run": () => ({
        run_id: 61,
        feature: RENEWAL,
        text: "Hello Pat",
        data: { subject: "Your renewal", body: "Hello Pat", removed_numbers: ["30"], tone: "retention" },
        ai_generated: true,
        citations: [],
        flags: ["numbers_removed"],
        proposals: [
          {
            id: 91,
            kind: "message",
            title: "Renewal e-mail to Acme",
            res_model: "sale.quotation",
            res_id: 9,
            payload: { subject: "Your renewal", body: "Hello Pat" },
            original: null,
            state: "pending",
            reason: null,
            ai_generated: true,
            run_id: 61,
            feature: RENEWAL,
          },
        ],
        status: "ok",
      }),
      "POST /api/v1/ai/proposals/91/accept": () => {
        accepted.push(91);
        return { id: 91, kind: "message", state: "accepted", ai_generated: false, payload: {}, title: "x" };
      },
    });
    panel();
    // the churn feature is off: no score request, only the draft button
    await userEvent.click(await screen.findByRole("button", { name: "Draft renewal" }));
    expect(await screen.findByText("Your renewal", { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("note", { name: /AI-drafted/ })).toBeInTheDocument();
    expect(screen.getByText(/were removed: 30/)).toBeInTheDocument();
    expect(accepted).toEqual([]);
    const card = screen.getByRole("region", { name: "Renewal e-mail to Acme" });
    await userEvent.click(within(card).getByRole("button", { name: "Send" }));
    await waitFor(() => expect(accepted).toEqual([91]));
  });

  it("is hidden when the features are off or the order is not a subscription", async () => {
    const { calls } = mockFetch({ "GET /api/v1/ai/status": status([]) });
    const { container } = panel();
    await waitFor(() => expect(container.querySelector(".ai-panel-hidden")).not.toBeNull());
    expect(calls.some((c) => c.path.includes("/sale_subscription/ai/"))).toBe(false);
    const other = panel({ plan_id: false, subscription_state: "draft" });
    expect(other.container.querySelector(".ai-panel-hidden")).not.toBeNull();
  });

  it("says AI is unavailable for drafts when the gateway is down, and labels risks", async () => {
    mockFetch({ "GET /api/v1/ai/status": status([RENEWAL], false) });
    panel();
    expect(await screen.findByText("AI unavailable")).toBeInTheDocument();
    expect(riskLabel("medium")).toBe("At risk");
    expect(riskLabel("low")).toBe("Low churn risk");
    expect(riskLabel(null)).toBe("Not scored yet");
  });

  it("shows a feature_disabled error from the score endpoint", async () => {
    mockFetch({
      "GET /api/v1/ai/status": status([CHURN]),
      "GET /api/v1/sale_subscription/ai/subscriptions/9/churn": () =>
        new Response(JSON.stringify({ code: "feature_disabled", message: "off" }), { status: 404 }),
    });
    panel();
    expect(await screen.findByText("This AI feature is turned off.")).toBeInTheDocument();
  });
});
