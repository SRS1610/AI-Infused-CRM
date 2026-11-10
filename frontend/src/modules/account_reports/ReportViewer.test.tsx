import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { clientAction } from "../../kernel/extensions";
import { mockFetch } from "../../test/mockApi";
import type { ReportData, ReportLine } from "./api";
import { reportQuery, reportsApi } from "./api";
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

const line = (
  id: string,
  name: string,
  values: (number | string | null)[],
  extra: Partial<ReportLine> = {},
): ReportLine => ({
  id,
  parent_id: null,
  code: null,
  name,
  level: 0,
  style: "normal",
  values,
  domains: values.map(() => null),
  unfoldable: false,
  unfolded: false,
  ...extra,
});

function balanceSheet(url: URL): ReportData {
  const unfold = url.searchParams.get("unfold") ?? "";
  const comparison = url.searchParams.get("comparison");
  const columns = [{ key: "p0", label: "As of 2031-03-31", type: "monetary" as const, date_to: "2031-03-31" }];
  if (comparison)
    columns.push({ key: "p1", label: "As of 2030-03-31", type: "monetary" as const, date_to: "2030-03-31" });
  const v = (a: number, b: number) => (comparison ? [a, b] : [a]);
  const bank = line("line:3", "Bank and cash", v(13400, 9000), {
    parent_id: "line:1",
    level: 1,
    unfoldable: true,
    unfolded: unfold.includes("line:3"),
    domains: v(1, 1).map(() => [["account_id", "in", [7]]]),
  });
  const lines = [line("line:1", "Assets", v(14000, 9500), { style: "total", code: "ASSETS" }), bank];
  if (unfold.includes("line:3"))
    lines.push(
      line("line:3:acc:1000", "1000 Bank", v(13400, 9000), {
        parent_id: "line:3",
        level: 2,
        style: "account",
        domains: v(1, 1).map(() => [["account_id", "=", 7]]),
      }),
    );
  lines.push(line("line:9", "Liabilities and equity", v(14000, 9500), { style: "total" }));
  return {
    report: {
      code: "balance_sheet",
      requested: "balance_sheet",
      name: "Balance sheet",
      engine: "lines",
      date_mode: "as_of",
      allow_comparison: true,
    },
    periods: [{ date_from: null, date_to: "2031-03-31" }],
    currency: { name: "USD", symbol: "$", decimal_places: 2 },
    companies: [{ id: 1, name: "My Company" }],
    consolidated: false,
    columns,
    lines,
    warnings: [],
  };
}

function generalLedger(url: URL): ReportData {
  const expand = url.searchParams.get("expand");
  const columns = [
    { key: "date", label: "Date", type: "date" as const },
    { key: "entry", label: "Entry", type: "text" as const },
    { key: "third", label: "Partner", type: "text" as const },
    { key: "label", label: "Label", type: "text" as const },
    { key: "debit", label: "Debit", type: "monetary" as const },
    { key: "credit", label: "Credit", type: "monetary" as const },
    { key: "balance", label: "Balance", type: "monetary" as const },
  ];
  const item = (n: number) =>
    line(`acc:1000:item:${n}`, `MISC/${n}`, ["2031-01-0" + n, `MISC/${n}`, "", "cash", 10, null, 10 * n], {
      parent_id: "acc:1000",
      level: 1,
      style: "item",
      move_id: 100 + n,
    });
  const base = {
    report: {
      code: "general_ledger",
      requested: "general_ledger",
      name: "General ledger",
      engine: "general_ledger",
      date_mode: "range" as const,
      allow_comparison: false,
    },
    periods: [{ date_from: "2031-01-01", date_to: "2031-03-31" }],
    currency: { name: "USD", symbol: "$", decimal_places: 2 },
    companies: [{ id: 1, name: "My Company" }],
    consolidated: false,
    columns,
    warnings: [],
  };
  if (expand) return { ...base, lines: [item(3)] };
  return {
    ...base,
    lines: [
      line("acc:1000", "1000 Bank", [null, null, null, null, 30, 0, 30], {
        unfoldable: true,
        unfolded: true,
        style: "account",
      }),
      item(1),
      item(2),
      line("acc:1000:more", "Load more…", [null, null, null, null, null, null, null], {
        parent_id: "acc:1000",
        level: 1,
        style: "load_more",
        next_offset: 2,
        remaining: 1,
      }),
      line("total", "Total", [null, null, null, null, 30, 30, 0], { style: "total" }),
    ],
  };
}

const session = () => ({
  allowed_companies: [
    { id: 1, name: "My Company" },
    { id: 2, name: "My Company (Europe)" },
  ],
});

describe("account reports viewer", () => {
  it("builds the query string of the report parameters", () => {
    expect(reportQuery({ comparison: "none", periods: 2, company_ids: [1, 2], unfold: ["a", "b"] })).toMatchObject({
      comparison: undefined,
      periods: undefined,
      company_ids: "1,2",
      unfold: "a,b",
    });
    const url = reportsApi.exportUrl("trial_balance", { date_to: "2031-03-31", unfold: ["x"] }, "xlsx");
    expect(url).toBe("/api/v1/account/reports/trial_balance/export?format=xlsx&date_to=2031-03-31");
  });

  it("renders a report, unfolds lines, compares periods, drills down and is accessible", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/account/reports/balance_sheet": (url) => balanceSheet(url),
      "GET /api/v1/auth/session": session,
      "GET /api/v1/models/account.move_line": () => ({
        items: [
          {
            id: 5,
            date: "2031-01-02",
            move_id: { id: 42, display_name: "MISC/2031/00001" },
            account_id: { id: 7, display_name: "1000 Bank" },
            partner_id: null,
            name: "capital",
            debit: 13400,
            credit: 0,
            balance: 13400,
          },
        ],
        total: 1,
        limit: 80,
        offset: 0,
      }),
    });
    const { container } = wrap(<ReportViewer code="balance_sheet" />);
    expect(await screen.findByRole("heading", { name: "Balance sheet" })).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "Assets" })).toBeInTheDocument();
    expect(screen.queryByLabelText("From")).not.toBeInTheDocument(); // as-of report
    const results = await axe.run(container, {
      runOnly: ["wcag2a", "wcag2aa"],
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations.map((v) => v.id)).toEqual([]);

    const toggle = screen.getByRole("button", { name: "Bank and cash" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    expect(await screen.findByRole("rowheader", { name: "1000 Bank" })).toBeInTheDocument();
    expect(calls.some((c) => c.url.searchParams.get("unfold") === "line:3")).toBe(true);

    await userEvent.selectOptions(screen.getByLabelText("Compare with"), "previous_year");
    expect(await screen.findByRole("columnheader", { name: "As of 2030-03-31" })).toBeInTheDocument();
    expect(calls.some((c) => c.url.searchParams.get("comparison") === "previous_year")).toBe(true);

    await userEvent.click(screen.getAllByRole("button", { name: /Journal items of 1000 Bank/ })[0]);
    const panel = await screen.findByRole("region", { name: /1000 Bank/ });
    expect(await within(panel).findByRole("link", { name: "MISC/2031/00001" })).toHaveAttribute(
      "href",
      "/m/account.move/42",
    );
    await userEvent.click(within(panel).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("region", { name: /1000 Bank/ })).not.toBeInTheDocument();

    await userEvent.click(screen.getByLabelText("My Company (Europe)"));
    await userEvent.click(screen.getByLabelText("My Company"));
    await userEvent.type(screen.getByLabelText("Currency"), "eur");
    await waitFor(() =>
      expect(
        calls.some(
          (c) => c.url.searchParams.get("company_ids") === "2,1" && c.url.searchParams.get("currency") === "EUR",
        ),
      ).toBe(true),
    );
    const exports = screen.getByRole("group", { name: "Export" });
    expect(within(exports).getByRole("link", { name: "PDF" }).getAttribute("href")).toContain("format=pdf");
  });

  it("loads more ledger items and links items to their entries", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/account/reports/general_ledger": (url) => generalLedger(url),
      "GET /api/v1/auth/session": () => ({ allowed_companies: [{ id: 1, name: "My Company" }] }),
    });
    wrap(<ReportViewer code="general_ledger" />);
    expect(await screen.findByRole("link", { name: "MISC/1" })).toHaveAttribute("href", "/m/account.move/101");
    expect(screen.getByLabelText("From")).toBeInTheDocument();
    expect(screen.queryByLabelText("Compare with")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Load more (1 remaining)" }));
    expect(await screen.findByRole("link", { name: "MISC/3" })).toBeInTheDocument();
    const page = calls.find((c) => c.url.searchParams.get("expand") === "acc:1000");
    expect(page?.url.searchParams.get("offset")).toBe("2");
    await userEvent.click(screen.getByLabelText("Include draft entries"));
    await waitFor(() => expect(calls.some((c) => c.url.searchParams.get("include_draft") === "true")).toBe(true));
  });

  it("runs the report named by the client action's context", async () => {
    mockFetch({
      "GET /api/v1/web/actions/account_reports.action_balance_sheet": () => ({
        id: 1,
        kind: "client",
        client_tag: "account_reports.viewer",
        context: { report: "balance_sheet" },
      }),
      "GET /api/v1/web/actions/broken": () => ({
        id: 2,
        kind: "client",
        client_tag: "account_reports.viewer",
        context: {},
      }),
      "GET /api/v1/account/reports/balance_sheet": (url) => balanceSheet(url),
      "GET /api/v1/auth/session": session,
    });
    const render_ = clientAction("account_reports.viewer");
    expect(render_).toBeDefined();
    wrap(render_!(), "/a/account_reports.action_balance_sheet");
    expect(await screen.findByRole("heading", { name: "Balance sheet" })).toBeInTheDocument();
  });

  it("explains an action without a report and shows errors", async () => {
    mockFetch({
      "GET /api/v1/web/actions/broken": () => ({
        id: 2,
        kind: "client",
        client_tag: "account_reports.viewer",
        context: {},
      }),
      "GET /api/v1/account/reports/nothing": () =>
        new Response(JSON.stringify({ error: "MissingError", message: "Unknown report 'nothing'" }), { status: 404 }),
      "GET /api/v1/auth/session": session,
    });
    wrap(clientAction("account_reports.viewer")!(), "/a/broken");
    expect(await screen.findByRole("alert")).toHaveTextContent("This action does not name a report.");
  });

  it("shows the server's error message", async () => {
    mockFetch({
      "GET /api/v1/account/reports/nothing": () =>
        new Response(JSON.stringify({ error: "MissingError", message: "Unknown report 'nothing'" }), { status: 404 }),
      "GET /api/v1/auth/session": session,
    });
    wrap(<ReportViewer code="nothing" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Unknown report 'nothing'");
  });
});
