import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import type { Proposal } from "../../kernel/ai/api";
import { aiDraftFeature, formPanelsFor, searchAddonsFor } from "../../kernel/extensions";
import { mockFetch } from "../../test/mockApi";
import { CallNotes, LeadSuggestions, NlSearch, Objections, parseValue, toDomain } from "./AiAssist";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const status =
  (features: string[], available = true) =>
  () => ({
    available,
    features: Object.fromEntries(features.map((f) => [f, { title: f }])),
  });

const proposal = (over: Partial<Proposal> = {}): Proposal => ({
  id: 51,
  kind: "write",
  title: "Stage → Proposition",
  res_model: "crm.lead",
  res_id: 7,
  payload: { stage_id: 3 },
  original: { stage_id: 2 },
  state: "pending",
  reason: null,
  ai_generated: true,
  run_id: 9,
  feature: "crm.call_summary",
  ...over,
});

const EMEA = ["GB", "FR", "DE"];
const searchData = {
  match: "all",
  explanation: "Open opportunities above 10,000 in EMEA closing this quarter, owned by you.",
  conditions: [
    { field: "type", operator: "=", value: "opportunity" },
    { field: "stage_id.is_won", operator: "=", value: false },
    { field: "expected_revenue", operator: ">", value: 10000 },
    { field: "country_id.code", operator: "in", value: EMEA },
    { field: "date_deadline", operator: ">=", value: "2026-10-01" },
    { field: "date_deadline", operator: "<=", value: "2026-12-31" },
    { field: "user_id", operator: "=", value: "$uid" },
  ],
  domain: [],
  labels: [],
  fields: {
    type: { label: "Type", type: "selection" },
    "stage_id.is_won": { label: "Stage is won", type: "boolean" },
    expected_revenue: { label: "Expected revenue", type: "monetary" },
    "country_id.code": { label: "Country ISO code", type: "char" },
    date_deadline: { label: "Expected closing", type: "date" },
    user_id: { label: "Salesperson", type: "many2one" },
    probability: { label: "Probability", type: "float" },
  },
};

describe("CRM AI assist", () => {
  it("registers the composer draft, the search add-on and the lead form panels", () => {
    expect(aiDraftFeature("crm.lead")).toBe("crm.followup_draft");
    expect(searchAddonsFor("crm.lead").map((a) => a.key)).toContain("crm.nl_search");
    expect(formPanelsFor("crm.lead").map((p) => p.title)).toEqual(
      expect.arrayContaining(["AI suggestions", "Summarise call notes", "Objection handling"]),
    );
  });

  it("parses edited values and builds the domain", () => {
    expect(parseValue("10k", ">", "monetary")).toBe("10k");
    expect(parseValue("20000", ">", "monetary")).toBe(20000);
    expect(parseValue("GB, FR", "in", "char")).toEqual(["GB", "FR"]);
    expect(parseValue("$uid", "=", "many2one")).toBe("$uid");
    expect(parseValue("", "is_set", "char")).toBeNull();
    expect(
      toDomain(
        [
          { field: "a", operator: "=", value: 1 },
          { field: "b", operator: "is_set", value: null },
        ],
        "any",
      ),
    ).toEqual({
      any: [
        ["a", "=", 1],
        ["b", "is_set"],
      ],
    });
  });

  it("1.6-e: turns a sentence into a reviewable filter, edited then applied in one click", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(["crm.nl_search"]),
      "POST /api/v1/ai/features/crm.nl_search/run": () => ({
        run_id: 12,
        feature: "crm.nl_search",
        text: "",
        data: searchData,
        ai_generated: true,
        citations: [],
        flags: [],
        proposals: [],
        status: "ok",
      }),
    });
    const apply = vi.fn();
    wrap(<NlSearch apply={apply} />);
    await userEvent.click(await screen.findByRole("button", { name: "✦ Ask AI" }));
    await userEvent.type(
      screen.getByLabelText("Describe what you are looking for"),
      "open deals over 10k in EMEA closing this quarter owned by me",
    );
    await userEvent.click(screen.getByRole("button", { name: "Generate filter" }));
    const review = await screen.findByLabelText("Suggested filter");
    expect(within(review).getByRole("note", { name: /AI-suggested/ })).toBeInTheDocument();
    expect(calls.find((c) => c.path.endsWith("/crm.nl_search/run"))!.body).toEqual({
      inputs: { query: "open deals over 10k in EMEA closing this quarter owned by me" },
    });
    expect(screen.getByLabelText("Value of condition 4")).toHaveValue("GB, FR, DE");
    // the user corrects a field: the threshold becomes 20k, and the label goes away
    const revenue = screen.getByLabelText("Value of condition 3");
    await userEvent.clear(revenue);
    await userEvent.type(revenue, "20000");
    expect(within(review).queryByRole("note", { name: /AI-suggested/ })).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Field of condition 3"), "probability");
    await userEvent.click(screen.getByRole("button", { name: "Remove condition 2" }));
    await userEvent.click(screen.getByRole("button", { name: "Apply filter" }));
    expect(apply).toHaveBeenCalledWith([
      ["type", "=", "opportunity"],
      ["probability", ">", 20000],
      ["country_id.code", "in", EMEA],
      ["date_deadline", ">=", "2026-10-01"],
      ["date_deadline", "<=", "2026-12-31"],
      ["user_id", "=", "$uid"],
    ]);
    expect(screen.queryByLabelText("Suggested filter")).not.toBeInTheDocument();
  });

  it("hides the search add-on when disabled and says AI unavailable when the gateway is down", async () => {
    mockFetch({ "GET /api/v1/ai/status": status([]) });
    const { unmount } = wrap(<NlSearch apply={vi.fn()} />);
    await waitFor(() => expect(screen.queryByRole("button", { name: "✦ Ask AI" })).not.toBeInTheDocument());
    unmount();
    mockFetch({ "GET /api/v1/ai/status": status(["crm.nl_search"], false) });
    wrap(<NlSearch apply={vi.fn()} />);
    expect(await screen.findByText("AI unavailable")).toBeInTheDocument();
  });

  it("reports an unusable AI answer without breaking the search", async () => {
    mockFetch({
      "GET /api/v1/ai/status": status(["crm.nl_search"]),
      "POST /api/v1/ai/features/crm.nl_search/run": () =>
        new Response(JSON.stringify({ error: "InvalidOutput", code: "invalid_output", message: "bad field" }), {
          status: 422,
        }),
    });
    wrap(<NlSearch apply={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "✦ Ask AI" }));
    await userEvent.type(screen.getByLabelText("Describe what you are looking for"), "deals in EMEA");
    await userEvent.click(screen.getByRole("button", { name: "Generate filter" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be used");
  });

  it("summarises call notes in the background, then reviews proposals one by one with the audit trail", async () => {
    const run = {
      id: 9,
      feature: "crm.call_summary",
      prompt_version: "crm.call_summary@1",
      provider: "Local test provider (deterministic)",
      model: "mock-mid",
      status: "ok",
      error_code: null,
      decision: "pending",
      create_date: "2026-11-10T09:00:00",
      user: "Dana Demo",
      output_data: {
        summary: "Novak Foods now wants two cells.",
        key_points: ["Budget about 120k"],
        next_step: null,
      },
      tool_calls: [],
      citations: [],
      proposals: [
        proposal(),
        proposal({ id: 52, title: "Expected revenue → 120,000.00", payload: { expected_revenue: 120000 } }),
      ],
    };
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(["crm.call_summary"]),
      "GET /api/v1/ai/runs": () => [],
      "POST /api/v1/crm/ai/leads/7/call-summary": () => ({ job_id: 3, state: "queued" }),
      "GET /api/v1/crm/ai/jobs/3": () => ({ id: 3, state: "done", run_id: 9, status: "ok" }),
      "GET /api/v1/ai/runs/9": () => run,
      "POST /api/v1/ai/proposals/51/accept": () => proposal({ state: "accepted", ai_generated: false }),
      "POST /api/v1/ai/proposals/52/reject": () => proposal({ id: 52, state: "rejected" }),
    });
    const reload = vi.fn();
    wrap(<CallNotes resId={7} reload={reload} />);
    await userEvent.type(await screen.findByLabelText("Call or meeting notes"), "Two cells now, about 120k.");
    await userEvent.click(screen.getByRole("button", { name: "Summarise" }));
    expect(await screen.findByText("Novak Foods now wants two cells.")).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/v1/crm/ai/leads/7/call-summary")!.body).toEqual({
      notes: "Two cells now, about 120k.",
    });
    expect(screen.getAllByRole("note", { name: /AI-drafted/ }).length).toBeGreaterThan(0);
    const stage = screen.getByRole("region", { name: "Stage → Proposition" });
    await userEvent.click(within(stage).getByRole("button", { name: "Accept" }));
    const revenue = screen.getByRole("region", { name: "Expected revenue → 120,000.00" });
    await userEvent.click(within(revenue).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(2));
    expect(calls.some((c) => c.path === "/api/v1/ai/proposals/52/accept")).toBe(false);
    const audit = screen.getByLabelText("AI run audit");
    expect(within(audit).getByText("crm.call_summary@1")).toBeInTheDocument();
    expect(within(audit).getByText(/#9/)).toBeInTheDocument();
  });

  it("suggests objection responses citing past deals; editing removes the AI label", async () => {
    mockFetch({
      "GET /api/v1/ai/status": status(["crm.objections"]),
      "POST /api/v1/ai/features/crm.objections/run": () => ({
        run_id: 4,
        feature: "crm.objections",
        text: "",
        data: {
          responses: [{ approach: "Payback", response: "Let's build the payback case.", citations: ["crm.lead:21"] }],
          citations: ["crm.lead:21"],
          insufficient_evidence: false,
        },
        ai_generated: true,
        citations: [{ id: "crm.lead:21", res_model: "crm.lead", res_id: 21, title: "Harlow: cobot cells" }],
        flags: [],
        proposals: [],
        status: "ok",
      }),
    });
    wrap(<Objections resId={7} />);
    await userEvent.type(await screen.findByLabelText("What did the customer object?"), "Too expensive");
    await userEvent.click(screen.getByRole("button", { name: "Suggest responses" }));
    expect(await screen.findByRole("link", { name: "Harlow: cobot cells" })).toHaveAttribute("href", "/m/crm.lead/21");
    expect(screen.getByRole("note", { name: /AI-suggested/ })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Response: Payback"), " Shall we?");
    expect(screen.queryByRole("note", { name: /AI-suggested/ })).not.toBeInTheDocument();
  });

  it("shows extracted lead details to review, and nothing when there are none", async () => {
    mockFetch({
      "GET /api/v1/ai/status": status(["crm.lead_extract"]),
      "GET /api/v1/ai/proposals": () => [
        proposal({ id: 60, feature: "crm.lead_extract", title: "Fill 3 lead fields from the email" }),
        proposal({ id: 61, feature: "crm.call_summary" }),
      ],
    });
    const { unmount } = wrap(<LeadSuggestions resId={7} />);
    expect(await screen.findByRole("region", { name: "Fill 3 lead fields from the email" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Stage → Proposition" })).not.toBeInTheDocument();
    unmount();
    mockFetch({ "GET /api/v1/ai/status": status(["crm.lead_extract"]), "GET /api/v1/ai/proposals": () => [] });
    const { container } = wrap(<LeadSuggestions resId={7} />);
    await waitFor(() => expect(container.querySelector(".ai-panel-hidden")).not.toBeNull());
  });
});
