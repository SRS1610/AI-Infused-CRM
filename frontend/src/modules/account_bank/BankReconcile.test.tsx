import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { clientAction } from "../../kernel/extensions";
import { mockFetch } from "../../test/mockApi";
import "./BankReconcile";
import type { BankLine, Candidate, OpenItem } from "./BankReconcile";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const line: BankLine = {
  id: 7,
  date: "2026-03-10",
  payment_ref: "Payment INV/2026/00012",
  ref: "E2E-1",
  partner: { id: 3, name: "Lyon Robotics SA" },
  partner_name: "Lyon Robotics SA",
  amount: 1200,
  amount_residual: 1200,
  currency: "EUR",
  state: "posted",
  journal: "Bank (BNK1)",
  statement: "BNK1 2026-03-10",
  rule: null,
};

const item: OpenItem = {
  id: 55,
  move_name: "INV/2026/00012",
  kind: "invoice",
  ref: null,
  date: "2026-02-10",
  date_maturity: "2026-03-12",
  partner: "Lyon Robotics SA",
  account: "1100 Accounts receivable",
  amount_residual: 1200,
  amount_residual_currency: 1200,
  currency: "EUR",
};

const suggestion: Candidate = {
  kind: "items",
  line_ids: [55],
  items: [item],
  amount: 1200,
  score: 1,
  reasons: [
    { code: "reference", message: "The label mentions the document's reference" },
    { code: "amount_exact", message: "Same amount" },
  ],
  model: { id: 1, name: "Invoices, bills and payments" },
  writeoffs: [],
};

function routes(extra: Record<string, (url: URL, init: RequestInit) => unknown> = {}) {
  return mockFetch({
    "GET /api/v1/models/account.journal": () => ({ items: [{ id: 1, display_name: "Bank (BNK1)" }], total: 1 }),
    "POST /api/v1/models/account.bank_statement_line/call/get_reconcile_lines": () => ({ result: [line] }),
    "POST /api/v1/models/account.bank_statement_line/7/call/get_reconcile_info": () => ({
      result: {
        ...line,
        narration: "SEPA credit transfer",
        counterparts: [
          { id: 1, account: "1040 Suspense", label: "x", partner: null, balance: -1200, suspense: true, matched: [] },
        ],
      },
    }),
    "POST /api/v1/models/account.bank_statement_line/7/call/get_suggestions": () => ({ result: [suggestion] }),
    "POST /api/v1/models/account.bank_statement_line/7/call/search_candidates": () => ({ result: [item] }),
    "POST /api/v1/models/account.bank_statement_line/7/call/action_match": () => ({ result: { state: "reconciled" } }),
    ...extra,
  });
}

describe("bank reconciliation", () => {
  it("lists transactions and accepts a suggestion", async () => {
    const { calls } = routes();
    wrap(clientAction("account_bank.reconcile")!());
    const button = await screen.findByRole("button", { name: /Payment INV\/2026\/00012/ });
    await userEvent.click(button);
    expect(await screen.findByText("100 %")).toBeInTheDocument();
    expect(screen.getByText(/mentions the document's reference/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/7/call/action_match"))).toBe(true));
    const match = calls.find((c) => c.path.endsWith("/7/call/action_match"))!;
    expect(match.body.kwargs.items).toEqual([{ line_id: 55 }]);
    expect(await screen.findByText("Transaction matched")).toBeInTheDocument();
  });

  it("matches searched items with a partial amount and writes off", async () => {
    const { calls } = routes({
      "GET /api/v1/models/account.account/name_search": () => [{ id: 9, display_name: "6700 Bank fees" }],
    });
    wrap(clientAction("account_bank.reconcile")!());
    await userEvent.click(await screen.findByRole("button", { name: /Payment INV\/2026\/00012/ }));
    await userEvent.click(await screen.findByRole("checkbox", { name: "Select INV/2026/00012" }));
    await userEvent.type(screen.getByLabelText("Amount to match for INV/2026/00012"), "200");
    await userEvent.click(screen.getByRole("button", { name: "Match selected items" }));
    await waitFor(() => expect(calls.filter((c) => c.path.endsWith("/action_match"))).toHaveLength(1));
    expect(calls.find((c) => c.path.endsWith("/action_match"))!.body.kwargs.items).toEqual([
      { line_id: 55, amount: 200 },
    ]);
    await userEvent.type(screen.getByLabelText("Account"), "6700");
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("account.account/name_search"))).toBe(true));
    await userEvent.clear(screen.getByLabelText("Account"));
    await userEvent.type(screen.getByLabelText("Account"), "6700 Bank fees");
    await userEvent.click(screen.getByRole("button", { name: "Write off the rest" }));
    await waitFor(() => expect(calls.filter((c) => c.path.endsWith("/action_match"))).toHaveLength(2));
    expect(calls.filter((c) => c.path.endsWith("/action_match"))[1].body.kwargs.writeoffs[0].account_id).toBe(9);
  });

  it("undoes a match, auto-reconciles and reports errors", async () => {
    const reconciled = { ...line, state: "reconciled", amount_residual: 0 };
    const { calls } = routes({
      "POST /api/v1/models/account.bank_statement_line/7/call/get_reconcile_info": () => ({
        result: {
          ...reconciled,
          narration: null,
          counterparts: [
            {
              id: 2,
              account: "1100 Accounts receivable",
              label: "INV/2026/00012",
              partner: "Lyon",
              balance: -1200,
              suspense: false,
              matched: [{ id: 55, move: "INV/2026/00012", move_id: 12 }],
            },
          ],
        },
      }),
      "POST /api/v1/models/account.bank_statement_line/7/call/action_unmatch": () => ({ result: true }),
      "POST /api/v1/models/account.bank_statement_line/call/action_auto_reconcile_all": () => ({
        result: { lines: 4, reconciled: 3 },
      }),
      "POST /api/v1/account_bank/import": () =>
        new Response(JSON.stringify({ error: "UserError", message: "Unrecognised statement format" }), { status: 400 }),
      "POST /api/v1/attachments": () => ({ id: 77 }),
    });
    wrap(clientAction("account_bank.reconcile")!());
    await userEvent.click(await screen.findByRole("button", { name: /Payment INV\/2026\/00012/ }));
    expect(await screen.findByText(/INV\/2026\/00012 ·/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/action_unmatch"))).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: "Auto-reconcile" }));
    expect(await screen.findByText("3 of 4 transactions reconciled by the rules")).toBeInTheDocument();
    const file = new File(["x"], "statement.csv", { type: "text/csv" });
    await userEvent.upload(screen.getByLabelText("Statement file"), file);
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose the bank journal first");
    await userEvent.selectOptions(screen.getByLabelText("Journal"), "1");
    await userEvent.upload(screen.getByLabelText("Statement file"), file);
    expect(await screen.findByText("Unrecognised statement format")).toBeInTheDocument();
  });
});
