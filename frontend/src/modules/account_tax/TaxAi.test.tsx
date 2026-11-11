import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { formPanelsFor } from "../../kernel/extensions";
import { PREFILING } from "./TaxAi";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const info = {
  title: "Pre-filing anomaly checks",
  task_class: "classify",
  interactive: true,
  writes: false,
  outbound: false,
  disclosure: "ai_generated",
  risk_class: "limited",
};
const status = (on = true, available = true) => ({ available, features: on ? { [PREFILING]: info } : {} });

function panel(record: Record<string, unknown> = { state: "draft" }) {
  const p = formPanelsFor("account.tax_return").find((x) => x.title === "Pre-filing checks (AI)");
  expect(p).toBeDefined();
  return wrap(p!.render({ model: "account.tax_return", resId: 3, record, reload: () => undefined }));
}

const review = {
  run_id: 8,
  net: 1300,
  items: [
    {
      rank: 1,
      code: "report_ledger_mismatch",
      severity: "error",
      title: "Return does not tie to the tax accounts",
      message: "The return (net 1300.0) differs from the tax accounts' movement (950.0) by 350.0",
      why: "The return and the tax accounts disagree.",
      action: "Find the manual entries on tax accounts.",
      model: null,
      res_ids: [],
      source: "check",
    },
    {
      rank: 2,
      code: "rate_outlier_for_country",
      severity: "warning",
      title: "Tax rate never used for this country before",
      message: "1 customer invoices use a tax rate never used for their country before",
      why: "Customers of this country were never charged this rate.",
      action: "Open the listed invoices.",
      model: "account.move",
      res_ids: [41, 42, 43, 44, 45, 46],
      source: "statistics",
    },
  ],
};

describe("Pre-filing checks on a tax return", () => {
  it("ranks the findings, links the documents and briefs with the AI label", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "GET /api/v1/account_tax/ai/returns/3/prefiling": () => review,
      "POST /api/v1/account_tax/ai/returns/3/prefiling/narrative": () => ({
        run_id: 9,
        narrative: "Fix the manual entry on the VAT account first.",
        removed_numbers: [],
      }),
    });
    panel();
    expect(await screen.findByText("Return does not tie to the tax accounts")).toBeInTheDocument();
    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("Error");
    expect(items[1]).toHaveTextContent("Warning");
    expect(screen.getByText("✦ Compared with previous periods")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "#41" })).toHaveAttribute("href", "/m/account.move/41");
    expect(screen.getByText("+1 more")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Brief me" }));
    expect(await screen.findByText(/manual entry on the VAT account/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(calls.filter((c) => c.path.endsWith("/prefiling")).length).toBe(2));
  });

  it("says when there is nothing to review and hides on closed returns or when off", async () => {
    mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "GET /api/v1/account_tax/ai/returns/3/prefiling": () => ({ run_id: 1, net: 0, items: [] }),
    });
    const open = panel();
    expect(await screen.findByText("Nothing to review before filing.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Brief me" })).not.toBeInTheDocument();
    open.unmount();
    const closed = panel({ state: "closed" });
    expect(closed.container.querySelector(".ai-panel-hidden")).not.toBeNull();
    closed.unmount();
    mockFetch({ "GET /api/v1/ai/status": () => status(false) });
    const off = panel();
    await waitFor(() => expect(off.container.querySelector(".ai-panel-hidden")).not.toBeNull());
  });

  it("shows server errors and when AI is unavailable", async () => {
    mockFetch({
      "GET /api/v1/ai/status": () => status(true, false),
      "GET /api/v1/account_tax/ai/returns/3/prefiling": () => review,
    });
    const { unmount } = panel();
    expect(await screen.findByText("AI unavailable")).toBeInTheDocument();
    unmount();
    mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "GET /api/v1/account_tax/ai/returns/3/prefiling": () =>
        new Response(JSON.stringify({ code: "feature_disabled", message: "off" }), { status: 404 }),
    });
    panel();
    expect(await screen.findByText("This AI feature is turned off.")).toBeInTheDocument();
  });
});
