import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { formPanelsFor } from "../../kernel/extensions";
import { CHECK, CLASSIFY } from "./AssetAi";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const info = {
  title: "x",
  task_class: "classify",
  interactive: true,
  writes: true,
  outbound: false,
  disclosure: "",
  risk_class: "minimal",
};
const status = (features: string[], available = true) => ({
  available,
  features: Object.fromEntries(features.map((f) => [f, info])),
});

function panel(model: string, title: string, record: Record<string, unknown>) {
  const p = formPanelsFor(model).find((x) => x.title === title);
  expect(p).toBeDefined();
  return wrap(p!.render({ model, resId: 7, record, reload: () => undefined }));
}

const proposal = {
  id: 31,
  kind: "action",
  title: "Classify 'Dell Latitude laptop' as Computer equipment (3 years)",
  res_model: "account.move_line",
  res_id: 70,
  payload: { action: "account_asset.apply_model", args: { model_id: 4 } },
  original: null,
  state: "pending",
  reason: null,
  ai_generated: true,
  run_id: 5,
  feature: CLASSIFY,
};

describe("Asset classification on a vendor bill", () => {
  it("suggests a model per line and lets the user accept the proposal", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": () => status([CLASSIFY]),
      "POST /api/v1/account_asset/ai/bills/7/classify": () => ({
        rows: [
          {
            line_id: 70,
            text: "Dell Latitude laptop",
            suggestion: { model_id: 4, name: "Computer equipment (3 years)", score: 0.7 },
            proposal,
          },
          { line_id: 71, text: "Delivery costs", suggestion: null, proposal: null },
        ],
        ai_generated: true,
      }),
      "POST /api/v1/ai/proposals/31/accept": () => ({ ...proposal, state: "accepted", ai_generated: false }),
    });
    panel("account.move", "Asset classification (AI)", { move_type: "in_invoice" });
    await userEvent.click(await screen.findByRole("button", { name: "Suggest asset models" }));
    expect((await screen.findAllByText(/→ Computer equipment \(3 years\)/)).length).toBeGreaterThan(0);
    expect(screen.getByText("No asset model fits this line.")).toBeInTheDocument();
    expect(screen.getByText("✦ AI suggestion")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/ai/proposals/31/accept")).toBe(true));
  });

  it("is hidden on customer invoices and when the feature is off", async () => {
    mockFetch({ "GET /api/v1/ai/status": () => status([CLASSIFY]) });
    const { container, unmount } = panel("account.move", "Asset classification (AI)", { move_type: "out_invoice" });
    expect(container.querySelector(".ai-panel-hidden")).not.toBeNull();
    unmount();
    mockFetch({ "GET /api/v1/ai/status": () => status([]) });
    const off = panel("account.move", "Asset classification (AI)", { move_type: "in_invoice" });
    await waitFor(() => expect(off.container.querySelector(".ai-panel-hidden")).not.toBeNull());
    expect(screen.queryByRole("button", { name: "Suggest asset models" })).not.toBeInTheDocument();
  });

  it("shows why suggestions could not be made", async () => {
    mockFetch({
      "GET /api/v1/ai/status": () => status([CLASSIFY], false),
      "POST /api/v1/account_asset/ai/bills/7/classify": () =>
        new Response(JSON.stringify({ code: "feature_disabled", message: "off" }), { status: 404 }),
    });
    panel("account.move", "Asset classification (AI)", { move_type: "in_invoice" });
    expect(await screen.findByText("AI unavailable")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Suggest asset models" }));
    expect(await screen.findByText("This AI feature is turned off.")).toBeInTheDocument();
  });
});

describe("Depreciation checks on an asset", () => {
  it("lists the findings and explains them with the AI label", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": () => status([CHECK]),
      "GET /api/v1/account_asset/ai/assets/7/check": () => ({
        run_id: 3,
        flagged: true,
        issues: [
          { code: "duplicate_posting", message: "Two depreciation entries fall in the same period", severity: "error" },
          {
            code: "salvage_high",
            message: "The salvage value is more than half of the original value",
            severity: "warning",
          },
        ],
      }),
      "POST /api/v1/account_asset/ai/assets/7/explain": () => ({
        run_id: 4,
        text: "The entry of March was posted twice.",
        removed_numbers: [],
      }),
    });
    panel("account.asset", "Depreciation checks (AI)", { state: "open" });
    expect(await screen.findByText(/Two depreciation entries fall in the same period/)).toBeInTheDocument();
    expect(screen.getByText("Error")).toBeInTheDocument();
    expect(screen.getByText("Warning")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Explain" }));
    expect(await screen.findByText(/posted twice/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(calls.filter((c) => c.path.endsWith("/check")).length).toBe(2));
  });

  it("says when nothing was found, and is hidden on asset models", async () => {
    mockFetch({
      "GET /api/v1/ai/status": () => status([CHECK]),
      "GET /api/v1/account_asset/ai/assets/7/check": () => ({ run_id: 3, flagged: false, issues: [] }),
    });
    const { unmount } = panel("account.asset", "Depreciation checks (AI)", { state: "open" });
    expect(await screen.findByText("No depreciation anomaly found.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Explain" })).not.toBeInTheDocument();
    unmount();
    const model = panel("account.asset", "Depreciation checks (AI)", { state: "model" });
    expect(model.container.querySelector(".ai-panel-hidden")).not.toBeNull();
  });
});
