import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { clientAction } from "../../kernel/extensions";
import { mockFetch } from "../../test/mockApi";
import type { ReportData } from "./api";
import { NL_REPORT, filtersOf, type AskedParams } from "./ReportAsk";
import { ReportViewer } from "./ReportViewer";

const wrap = (ui: React.ReactNode, path = "/") =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/a/:actionRef" element={ui} />
          <Route path="*" element={ui} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );

const report = (code: string, name: string): ReportData => ({
  report: { code, requested: code, name, engine: "lines", date_mode: "range", allow_comparison: true },
  periods: [{ date_from: "2026-07-01", date_to: "2026-09-30" }],
  currency: { name: "USD", symbol: "$", decimal_places: 2 },
  companies: [{ id: 1, name: "My Company" }],
  consolidated: false,
  columns: [{ key: "p0", label: "2026-07-01 - 2026-09-30", type: "monetary" }],
  lines: [],
  warnings: [],
});

const info = {
  title: "Natural-language reports",
  task_class: "nl_query",
  interactive: true,
  writes: false,
  outbound: false,
  disclosure: "",
  risk_class: "minimal",
};
const status = (on = true) => ({ available: true, features: on ? { [NL_REPORT]: info } : {} });
const session = () => ({ allowed_companies: [{ id: 1, name: "My Company" }] });

const answer = (code: string, name: string) => ({
  run_id: 12,
  feature: NL_REPORT,
  text: "",
  data: {
    params: {
      report: code,
      date_from: "2026-07-01",
      date_to: "2026-09-30",
      comparison: "previous_year",
      periods: 1,
      partner_ids: [5],
      journal_ids: [],
      company_ids: [],
      include_draft: false,
      account_type: "all",
    },
    report_name: name,
    partner_names: ["Acme Corp"],
    journal_names: [],
    company_names: [],
    warnings: ["Not found among the journals you can access: Petty cash"],
  },
  ai_generated: true,
  citations: [],
  flags: [],
  proposals: [],
  status: "ok",
});

describe("Ask box of the financial reports", () => {
  it("turns a question into filters the user reviews and applies", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "GET /api/v1/auth/session": session,
      "GET /api/v1/account/reports/profit_loss": () => report("profit_loss", "Profit and loss"),
      "POST /api/v1/ai/features/account_reports.nl_report/run": () => answer("profit_loss", "Profit and loss"),
    });
    wrap(<ReportViewer code="profit_loss" />);
    await userEvent.type(await screen.findByLabelText("Question about the accounts"), "P&L Q3 vs last year for Acme");
    await userEvent.click(screen.getByRole("button", { name: "Fill the filters" }));
    expect(await screen.findByText(/2026-07-01 → 2026-09-30/)).toBeInTheDocument();
    expect(screen.getByText(/compared with the previous year/)).toHaveTextContent("Acme Corp");
    expect(screen.getByText("Not found among the journals you can access: Petty cash")).toBeInTheDocument();
    const run = calls.find((c) => c.path.endsWith("nl_report/run"));
    expect(run?.body).toEqual({ inputs: { question: "P&L Q3 vs last year for Acme" } });
    // nothing changes before the user applies
    expect(calls.filter((c) => c.path === "/api/v1/account/reports/profit_loss")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Apply these filters" }));
    await waitFor(() => {
      const last = calls.filter((c) => c.path === "/api/v1/account/reports/profit_loss").at(-1)!;
      expect(last.url.searchParams.get("date_from")).toBe("2026-07-01");
      expect(last.url.searchParams.get("comparison")).toBe("previous_year");
      expect(last.url.searchParams.get("partner_ids")).toBe("5");
    });
    expect(screen.getByText("Only: Acme Corp")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(
        calls
          .filter((c) => c.path === "/api/v1/account/reports/profit_loss")
          .at(-1)!
          .url.searchParams.get("partner_ids"),
      ).toBeNull(),
    );
  });

  it("opens another report from the action when the question is about it", async () => {
    mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "GET /api/v1/auth/session": session,
      "GET /api/v1/web/actions/pl": () => ({
        id: 1,
        kind: "client",
        client_tag: "account_reports.viewer",
        context: { report: "profit_loss" },
      }),
      "GET /api/v1/account/reports/profit_loss": () => report("profit_loss", "Profit and loss"),
      "GET /api/v1/account/reports/aged_receivable": () => report("aged_receivable", "Aged receivables"),
      "POST /api/v1/ai/features/account_reports.nl_report/run": () => answer("aged_receivable", "Aged receivables"),
    });
    wrap(clientAction("account_reports.viewer")!(), "/a/pl");
    await userEvent.type(await screen.findByLabelText("Question about the accounts"), "who owes us money");
    await userEvent.click(screen.getByRole("button", { name: "Fill the filters" }));
    await userEvent.click(await screen.findByRole("button", { name: "Apply these filters" }));
    expect(await screen.findByRole("heading", { name: "Aged receivables" })).toBeInTheDocument();
  });

  it("is hidden when off; explains unusable answers and errors", async () => {
    mockFetch({
      "GET /api/v1/ai/status": () => status(false),
      "GET /api/v1/auth/session": session,
      "GET /api/v1/account/reports/profit_loss": () => report("profit_loss", "Profit and loss"),
    });
    const off = wrap(<ReportViewer code="profit_loss" />);
    await screen.findByRole("heading", { name: "Profit and loss" });
    expect(screen.queryByLabelText("Question about the accounts")).not.toBeInTheDocument();
    off.unmount();
    mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "GET /api/v1/auth/session": session,
      "GET /api/v1/account/reports/profit_loss": () => report("profit_loss", "Profit and loss"),
      "POST /api/v1/ai/features/account_reports.nl_report/run": () => ({
        ...answer("x", "x"),
        data: { params: null, warnings: ["Unknown or unavailable report 'tax_secret'"] },
      }),
    });
    const { unmount } = wrap(<ReportViewer code="profit_loss" />);
    await userEvent.type(await screen.findByLabelText("Question about the accounts"), "secret report");
    await userEvent.click(screen.getByRole("button", { name: "Fill the filters" }));
    expect(await screen.findByText("The question could not be turned into report filters.")).toBeInTheDocument();
    unmount();
    mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "GET /api/v1/auth/session": session,
      "GET /api/v1/account/reports/profit_loss": () => report("profit_loss", "Profit and loss"),
      "POST /api/v1/ai/features/account_reports.nl_report/run": () =>
        new Response(JSON.stringify({ code: "feature_disabled", message: "off" }), { status: 404 }),
    });
    wrap(<ReportViewer code="profit_loss" />);
    await userEvent.type(await screen.findByLabelText("Question about the accounts"), "sales");
    await userEvent.click(screen.getByRole("button", { name: "Fill the filters" }));
    expect(await screen.findByText("This AI feature is turned off.")).toBeInTheDocument();
    expect(filtersOf(answer("a", "b").data.params as AskedParams).partner_ids).toEqual([5]);
  });
});
