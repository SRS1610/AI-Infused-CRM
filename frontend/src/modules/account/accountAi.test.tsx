import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { MemoryRouter } from "react-router-dom";
import type { Proposal } from "../../kernel/ai/api";
import { clientAction, formPanelsFor, searchAddonsFor } from "../../kernel/extensions";
import { mockFetch } from "../../test/mockApi";
import { BillAnomalies } from "./BillAnomalies";
import { BillReader } from "./BillImport";
import { CashflowForecastPage, ForecastChart } from "./CashflowForecast";
import type { Forecast, ReadResult } from "./api";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const status =
  (features: string[], available = true) =>
  () => ({ available, features: Object.fromEntries(features.map((f) => [f, { title: f }])) });

const proposal: Proposal = {
  id: 70,
  kind: "action",
  title: "Create in_invoice from Imprimerie Dubois SARL (1 lines)",
  res_model: "account.move",
  res_id: null,
  payload: {
    action: "account.bill_from_document",
    args: { values: { invoice_line_ids: [["create", { name: "Toner", account_id: 11, tax_ids: [["set", [5]]] }]] } },
  },
  original: null,
  state: "pending",
  reason: null,
  ai_generated: true,
  run_id: 31,
  feature: "account.bill_ocr",
};

const reading = (over: Partial<ReadResult["data"]> = {}): ReadResult => ({
  run_id: 31,
  status: "ok",
  ai_generated: true,
  proposals: [proposal],
  data: {
    method: "text",
    move_type: "in_invoice",
    extraction: {
      partner_name: "Imprimerie Dubois SARL",
      partner_vat: "FR40303265045",
      iban: null,
      invoice_number: "F-2026-0412",
      invoice_date: "2026-03-14",
      due_date: "2026-04-13",
      currency: "EUR",
      amount_untaxed: 100,
      amount_tax: 20,
      amount_total: 120,
    },
    partner: { id: 4, display_name: "Imprimerie Dubois SARL", method: "vat", confidence: 1 },
    lines: [
      {
        description: "Toner",
        quantity: 1,
        unit_price: 100,
        amount: 100,
        tax_rate: 20,
        account_id: 11,
        account: "6000 General expenses",
        account_source: "history",
        tax_ids: [5],
        taxes: ["VAT 20% (purchases)"],
        tax_source: "rate",
        issue: null,
        confidence: 0.9,
        reason: 'Like "Toner" on BILL/2026/00003',
      },
    ],
    currency: "EUR",
    checks: { lines_total: 100, computed_tax: 20, differences: [] },
    needs_review: false,
    issues: [],
    warnings: [],
    anomalies: [],
    ...over,
  },
});

describe("accounting AI client", () => {
  it("registers the pages, the bill-list upload and the bill form panels", () => {
    expect(clientAction("account.bill_import")).toBeDefined();
    expect(clientAction("account.cashflow_forecast")).toBeDefined();
    expect(searchAddonsFor("account.move").map((a) => a.key)).toContain("account.bill_ocr");
    expect(
      formPanelsFor("account.move")
        .map((p) => p.title)
        .sort(),
    ).toEqual(["Anomaly checks", "Read from a document"]);
  });

  it("reads a document, shows every auto-coded line and creates the draft only on accept", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(["account.bill_ocr"]),
      "POST /api/v1/account/ai/bills/read": () => reading(),
      "GET /api/v1/models/account.account": () => ({
        items: [
          { id: 11, display_name: "6000 General expenses" },
          { id: 12, display_name: "6500 Software and subscriptions" },
        ],
        total: 2,
        limit: 200,
        offset: 0,
      }),
      "POST /api/v1/account/ai/bills/proposals/70/accept": () => ({
        proposal: { ...proposal, state: "edited", ai_generated: false },
        move_id: 99,
      }),
    });
    wrap(<BillReader moveType="in_invoice" />);
    const input = (await screen.findByLabelText("Read a document (PDF or image)")) as HTMLInputElement;
    await userEvent.upload(input, new File(["%PDF-1.7"], "bill.pdf", { type: "application/pdf" }));
    const review = await screen.findByRole("region", { name: "Document reading" });
    const audit = await axe.run(review, {
      runOnly: ["wcag2a", "wcag2aa"],
      rules: { "color-contrast": { enabled: false } },
    });
    expect(audit.violations.map((v) => v.id)).toEqual([]);
    expect(within(review).getByText("F-2026-0412")).toBeInTheDocument();
    expect(within(review).getByText("VAT 20% (purchases)")).toBeInTheDocument();
    expect(within(review).getByText(/partner history/)).toBeInTheDocument();
    expect(within(review).getByRole("status")).toHaveTextContent("Totals check");
    expect(calls.some((c) => c.path.endsWith("/accept"))).toBe(false);
    const read = calls.find((c) => c.path === "/api/v1/account/ai/bills/read");
    expect(read?.body).toBeInstanceOf(FormData);
    expect((read?.body as FormData).get("move_type")).toBe("in_invoice");
    await screen.findByRole("option", { name: "6500 Software and subscriptions" });
    await userEvent.selectOptions(screen.getByLabelText("Account for Toner"), "12");
    await userEvent.click(screen.getByRole("button", { name: "Accept edited" }));
    const accepted = calls.find((c) => c.path.endsWith("/accept"));
    expect(accepted?.body.payload.args.values.invoice_line_ids[0][1].account_id).toBe(12);
  });

  it("says when a reading needs review and warns about anomalies", async () => {
    mockFetch({
      "GET /api/v1/ai/status": status(["account.bill_ocr"]),
      "POST /api/v1/account/ai/bills/read": () =>
        reading({
          needs_review: true,
          checks: {
            lines_total: 100,
            computed_tax: 20,
            differences: [{ check: "total", document: 130, computed: 120, difference: -10 }],
          },
          issues: ["No 19% tax in this company"],
          anomalies: [
            {
              kind: "duplicate_number",
              severity: "high",
              explanation: "Same invoice number F-2026-0412",
              related_id: 8,
            },
          ],
        }),
      "GET /api/v1/models/account.account": () => ({ items: [], total: 0, limit: 200, offset: 0 }),
    });
    wrap(<BillReader />);
    const input = (await screen.findByLabelText("Read a document (PDF or image)")) as HTMLInputElement;
    await userEvent.upload(input, new File(["x"], "scan.jpg", { type: "image/jpeg" }));
    expect(await screen.findByText("Needs review")).toBeInTheDocument();
    expect(screen.getByText(/Lines \+ tax vs total/)).toBeInTheDocument();
    expect(screen.getByText("No 19% tax in this company")).toBeInTheDocument();
    expect(screen.getByRole("alert", { name: "Anomaly warnings" })).toHaveTextContent("Same invoice number");
    expect(screen.getByRole("link", { name: "Open the similar document" })).toHaveAttribute(
      "href",
      "/m/account.move/8",
    );
  });

  it("hides the upload when the feature is off", async () => {
    mockFetch({ "GET /api/v1/ai/status": status([]) });
    const { container } = wrap(<BillReader />);
    await new Promise((r) => setTimeout(r, 20));
    expect(container.querySelector("input[type=file]")).toBeNull();
  });

  it("lists anomaly flags and lets the user dismiss one", async () => {
    let state = "open";
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(["account.anomaly"]),
      "GET /api/v1/account/ai/moves/5/anomalies": () => ({
        flags: [
          {
            id: 3,
            kind: "duplicate_number",
            severity: "high",
            score: 1,
            explanation: "Same invoice number FO-102 as BILL/2026/00003.",
            state,
            related: { id: 8, name: "BILL/2026/00003" },
          },
        ],
      }),
      "POST /api/v1/account/ai/anomalies/3/dismiss": () => {
        state = "dismissed";
        return { id: 3, state };
      },
    });
    wrap(<BillAnomalies resId={5} />);
    const list = await screen.findByRole("list", { name: "Anomaly flags" });
    expect(within(list).getByText("Same invoice number")).toBeInTheDocument();
    expect(within(list).getByRole("link", { name: "BILL/2026/00003" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Not an anomaly" }));
    expect(calls.some((c) => c.path.endsWith("/dismiss"))).toBe(true);
    expect(await screen.findByText("dismissed")).toBeInTheDocument();
  });

  const forecast: Forecast = {
    today: "2026-11-10",
    currency: "EUR",
    opening: 10000,
    closing: 12500,
    lowest: { date: "2026-11-28", balance: 6200 },
    open_receivables: 8000,
    open_payables: 3000,
    largest_items: [{ label: "INV/2026/00042", date: "2026-11-20", amount: 4000, kind: "receivable" }],
    recurring: [{ label: "Salaries", amount: -5000, day: 28 }],
    summary: "Cash today is 10,000.00.",
    weeks: [1, 2].map((w) => ({
      week: w,
      start: `2026-11-1${w}`,
      end: `2026-11-1${w + 6}`,
      inflow: 4000,
      outflow: -2750,
      net: 1250,
      position: 10000 + 1250 * w,
      low: 9000 + 1250 * w,
      high: 11000 + 1250 * w,
    })),
  };

  it("draws the forecast with a text alternative and a data table", () => {
    wrap(<ForecastChart forecast={forecast} />);
    const chart = screen.getByRole("img", { name: /Forecast cash position by week/ });
    expect(chart).toHaveAccessibleName(expect.stringContaining("lowest 6,200.00 on 2026-11-28"));
  });

  it("shows the forecast page with the engine summary, then the checked narrative", async () => {
    mockFetch({
      "GET /api/v1/ai/status": status(["account.cashflow_forecast"]),
      "GET /api/v1/account/ai/cashflow": () => forecast,
      "POST /api/v1/account/ai/cashflow/narrative": () => ({
        run_id: 9,
        narrative: "Cash stands at 10,000.00 today.",
        narrative_source: "model",
        foreign_numbers: [],
      }),
    });
    const { container } = wrap(<CashflowForecastPage />);
    expect(await screen.findByText("Cash today is 10,000.00.")).toBeInTheDocument();
    const results = await axe.run(container, {
      runOnly: ["wcag2a", "wcag2aa"],
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations.map((v) => v.id)).toEqual([]);
    const table = screen.getByRole("table", { name: "Weekly cash position" });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    await userEvent.click(screen.getByRole("button", { name: "Explain the forecast" }));
    expect(await screen.findByText(/Cash stands at 10,000.00 today./)).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("AI-generated");
  });

  it("says when the forecast is switched off", async () => {
    mockFetch({ "GET /api/v1/ai/status": status([]) });
    wrap(<CashflowForecastPage />);
    expect(await screen.findByText("The cash-flow forecast is turned off for this company.")).toBeInTheDocument();
  });
});
