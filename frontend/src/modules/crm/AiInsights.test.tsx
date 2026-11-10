import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { clientAction, formPanelsFor, selectionActionsFor } from "../../kernel/extensions";
import type { Proposal } from "../../kernel/ai/api";
import "./CrmReports";
import "./AiInsights";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const ALL = ["crm.lead_score", "crm.deal_risk", "crm.next_best_action", "crm.lead_routing", "crm.forecast"];
const status =
  (available = true, names = ALL) =>
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
          outbound: false,
          disclosure: "ai_generated",
          risk_class: "limited",
        },
      ]),
    ),
  });

const proposal = (over: Partial<Proposal> = {}): Proposal => ({
  id: 70,
  kind: "write",
  title: "Assign Robot cell to Rep 1",
  res_model: "crm.lead",
  res_id: 5,
  payload: { user_id: 11 },
  original: { user_id: null },
  state: "pending",
  reason: null,
  ai_generated: true,
  run_id: 31,
  feature: "crm.lead_routing",
  ...over,
});

const insights = {
  lead_id: 5,
  score: {
    score: 72,
    probability: 0.72,
    factors: [
      { feature: "source=Referral", label: "Source: Referral", value: 1, direction: "positive", weight: 0.81 },
      { feature: "free_email", label: "Free email provider", value: 1, direction: "negative", weight: -0.4 },
    ],
    source: "model",
    scored_at: "2026-11-10T02:30:00Z",
    model: { version: 3, trained_at: "2026-11-10T02:20:00Z", rows: 412, holdout_auc: 0.84 },
    excluded_inputs:
      "gender, age, ethnicity, religion, disability, nationality and their proxies are never model inputs",
    run_id: 21,
  },
  risk: {
    level: "high",
    score: 0.85,
    reasons: [
      { code: "stale_activity", label: "No activity for 34 days", weight: 0.35 },
      { code: "close_date_passed", label: "Expected closing date passed 9 days ago", weight: 0.25 },
    ],
    checked_at: "2026-11-10T05:41:00Z",
    run_id: 22,
  },
};

function panel(record: Record<string, unknown> = { user_id: null }) {
  const p = formPanelsFor("crm.lead").find((x) => x.title === "AI insights");
  expect(p).toBeDefined();
  return wrap(p!.render({ model: "crm.lead", resId: 5, record, reload: () => undefined }));
}

describe("CRM AI insights", () => {
  it("shows the AI score with a why panel, the risk badge and feedback", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(),
      "GET /api/v1/crm/ai/leads/5/insights": () => insights,
      "POST /api/v1/ai/runs/21/feedback": () => ({ id: 1 }),
    });
    panel({ user_id: { id: 3, display_name: "Demo" } });
    expect(await screen.findByLabelText("AI score 72 out of 100")).toBeInTheDocument();
    expect(screen.getByText("Win probability 72 %")).toBeInTheDocument();
    expect(screen.getByText("Model v3, trained on 412 closed deals")).toBeInTheDocument();
    expect(screen.getAllByRole("note").map((n) => n.textContent)).toEqual(
      expect.arrayContaining(["✦ AI score", "✦ AI alert"]),
    );
    expect(screen.getByText("High risk")).toBeInTheDocument();
    expect(screen.getByText("No activity for 34 days")).toBeInTheDocument();
    const why = screen.getByRole("button", { name: "Why this score?" });
    expect(why).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(why);
    const region = screen.getByRole("region", { name: "Why this score" });
    expect(within(region).getByText("Source: Referral")).toBeInTheDocument();
    expect(within(region).getByText("Raises the score:")).toBeInTheDocument();
    expect(within(region).getByText("Lowers the score:")).toBeInTheDocument();
    expect(within(region).getByText(/never model inputs/)).toBeInTheDocument();
    await userEvent.click(within(region).getByTitle("Helpful"));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/ai/runs/21/feedback")).toBe(true));
    // the lead has a salesperson: no routing affordance
    expect(screen.queryByRole("button", { name: "Suggest salesperson" })).not.toBeInTheDocument();
  });

  it("is hidden when every insight feature is turned off", async () => {
    const { calls } = mockFetch({ "GET /api/v1/ai/status": status(true, []) });
    const { container } = panel();
    await waitFor(() => expect(container.querySelector(".ai-insights-hidden")).not.toBeNull());
    expect(screen.queryByText(/AI score/)).not.toBeInTheDocument();
    expect(calls.some((c) => c.path.includes("/crm/ai/"))).toBe(false);
  });

  it("says AI is unavailable for next-best-action when the gateway is down", async () => {
    mockFetch({
      "GET /api/v1/ai/status": status(false, ["crm.next_best_action"]),
    });
    panel();
    expect(await screen.findByText("AI unavailable")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Suggest next actions" })).not.toBeInTheDocument();
  });

  it("suggests next actions and schedules one only through a proposal", async () => {
    const accepted: number[] = [];
    mockFetch({
      "GET /api/v1/ai/status": status(true, ["crm.next_best_action"]),
      "POST /api/v1/ai/features/crm.next_best_action/run": () => ({
        run_id: 40,
        feature: "crm.next_best_action",
        text: "",
        data: {
          actions: [
            {
              title: "Call Jordan about the proposal",
              kind: "call",
              rationale: "No reply for a month.",
              signals: ["stale_activity"],
              due_in_days: 1,
            },
          ],
          citations: [],
        },
        ai_generated: true,
        citations: [],
        flags: [],
        proposals: [
          proposal({
            id: 81,
            kind: "action",
            title: "Schedule: Call Jordan about the proposal",
            payload: { action: "chatter.schedule_activity", args: { summary: "Call Jordan" } },
            feature: "crm.next_best_action",
          }),
        ],
        status: "ok",
      }),
      "POST /api/v1/ai/proposals/81/accept": () => {
        accepted.push(81);
        return proposal({ id: 81, kind: "action", state: "accepted", ai_generated: false });
      },
    });
    panel();
    await userEvent.click(await screen.findByRole("button", { name: "Suggest next actions" }));
    expect(await screen.findByText("Call Jordan about the proposal")).toBeInTheDocument();
    expect(screen.getByText("No reply for a month.")).toBeInTheDocument();
    expect(screen.getByText("✦ AI suggestions")).toBeInTheDocument();
    expect(accepted).toEqual([]);
    const card = screen.getByRole("region", { name: "Schedule: Call Jordan about the proposal" });
    await userEvent.click(within(card).getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(accepted).toEqual([81]));
  });

  it("proposes a salesperson for an unassigned lead", async () => {
    mockFetch({
      "GET /api/v1/ai/status": status(true, ["crm.lead_routing"]),
      "POST /api/v1/crm/ai/leads/5/routing": () => ({
        run_id: 31,
        rows: [
          {
            lead_id: 5,
            lead: "Robot cell",
            user_id: 11,
            user: "Rep 1",
            team_id: 2,
            team: "Sales",
            reasons: ["Rep 1 won 5 of 6 similar deals"],
            confidence: 0.8,
            status: "proposed",
            proposal: proposal(),
          },
        ],
      }),
    });
    panel({ user_id: false });
    await userEvent.click(await screen.findByRole("button", { name: "Suggest salesperson" }));
    expect(await screen.findByText("Rep 1 won 5 of 6 similar deals")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Assign Robot cell to Rep 1" })).toBeInTheDocument();
  });

  it("reviews bulk routing suggestions row by row", async () => {
    const decisions: string[] = [];
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(true, ["crm.lead_routing"]),
      "POST /api/v1/crm/ai/routing": () => ({
        run_id: 32,
        rows: [
          { lead_id: 5, lead: "Lead A", user_id: 11, user: "Rep 1", team_id: 2, team: "Sales", reasons: ["r1"],
            confidence: 0.7, status: "proposed", proposal: proposal({ id: 71 }) },
          { lead_id: 6, lead: "Lead B", user_id: 12, user: "Rep 2", team_id: 2, team: "Sales", reasons: ["r2"],
            confidence: 0.6, status: "proposed", proposal: proposal({ id: 72 }) },
          { lead_id: 7, lead: "Lead C", user_id: null, user: null, team_id: null, team: null, reasons: [],
            confidence: 0.5, status: "no_candidate", proposal: null },
        ],
      }), // prettier-ignore
      "POST /api/v1/ai/proposals/71/accept": () => {
        decisions.push("71:accept");
        return proposal({ id: 71, state: "accepted" });
      },
      "POST /api/v1/ai/proposals/72/reject": () => {
        decisions.push("72:reject");
        return proposal({ id: 72, state: "rejected" });
      },
    });
    const action = selectionActionsFor("crm.lead").find((a) => a.label === "Suggest salespeople (AI)");
    expect(action).toBeDefined();
    wrap(action!.render({ model: "crm.lead", ids: [5, 6, 7], onDone: () => undefined }));
    await userEvent.click(await screen.findByRole("button", { name: "Accept Rep 1 for Lead A" }));
    await userEvent.click(screen.getByRole("button", { name: "Reject suggestion for Lead B" }));
    await waitFor(() => expect(decisions).toEqual(["71:accept", "72:reject"]));
    expect(await screen.findByText("accepted")).toBeInTheDocument();
    expect(screen.getByText("rejected")).toBeInTheDocument();
    expect(screen.getByText("no candidate")).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/v1/crm/ai/routing")?.body).toEqual({ lead_ids: [5, 6, 7] });
  });

  it("adds the forecast with confidence range to the sales analysis", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(true, ["crm.forecast"]),
      "GET /api/v1/crm/ai/forecast": () => ({
        enabled: true,
        forecast: {
          id: 4,
          name: "Forecast November 2026",
          periods: [
            {
              period: 0,
              label: "November 2026",
              p10: 81000,
              p50: 102500,
              p90: 131000,
              explanation: "November 2026: most likely 102,500 (80% range 81,000–131,000).",
            },
          ],
          method: "Monte Carlo (4000 runs)",
          narrative: null,
          run_id: 55,
          generated_at: "2026-11-10T08:00:00Z",
        },
      }),
      "POST /api/v1/crm/ai/forecast": () => ({ job_id: 9, status: "queued" }),
      "GET /api/v1/crm/reports/velocity": () => null,
      "GET /api/v1/crm/reports/*": () => [],
    });
    wrap(clientAction("crm.reports")!());
    expect(await screen.findByRole("heading", { name: /Forecast with confidence range/ })).toBeInTheDocument();
    const row = screen.getByRole("rowheader", { name: "November 2026" }).closest("tr")!;
    expect(within(row).getByText("81,000")).toBeInTheDocument();
    expect(within(row).getByText("102,500")).toBeInTheDocument();
    expect(within(row).getByText("131,000")).toBeInTheDocument();
    expect(screen.getByText("✦ AI forecast")).toBeInTheDocument();
    expect(screen.getByText(/80% range/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Refresh forecast" }));
    expect(await screen.findByText(/notification when it is ready/)).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/v1/crm/ai/forecast")).toBe(true);
  });

  it("hides the forecast section when the feature is off", async () => {
    mockFetch({
      "GET /api/v1/ai/status": status(true, []),
      "GET /api/v1/crm/reports/velocity": () => null,
      "GET /api/v1/crm/reports/*": () => [],
    });
    wrap(clientAction("crm.reports")!());
    expect(await screen.findByRole("heading", { name: "Sales analysis" })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: /Forecast with confidence range/ })).not.toBeInTheDocument(),
    );
  });
});
