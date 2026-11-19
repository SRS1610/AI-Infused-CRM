import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { clientAction } from "../../kernel/extensions";
import { mockFetch } from "../../test/mockApi";
import "./BankReconcile";
import { RECONCILE } from "./BankAi";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const line = {
  id: 7,
  date: "2026-03-10",
  payment_ref: "VIR LYON ROBOTICS FACT 2026 12",
  ref: null,
  partner: null,
  partner_name: "LYON ROBOTICS",
  amount: 1200,
  amount_residual: 1200,
  currency: "EUR",
  state: "posted",
  journal: "Bank (BNK1)",
  statement: null,
  rule: null,
};

const info = {
  title: "Reconciliation suggestions",
  task_class: "classify",
  interactive: true,
  writes: true,
  outbound: false,
  disclosure: "",
  risk_class: "minimal",
};

const proposal = {
  id: 61,
  kind: "action",
  title: "Match VIR LYON ROBOTICS with INV/2026/00012",
  res_model: "account.bank_statement_line",
  res_id: 7,
  payload: { action: "account_bank.match", args: { items: [{ line_id: 55 }], writeoffs: [] } },
  original: null,
  state: "pending",
  reason: null,
  ai_generated: true,
  run_id: 8,
  feature: RECONCILE,
};

function routes(on = true, extra: Record<string, (url: URL, init: RequestInit) => unknown> = {}) {
  return mockFetch({
    "GET /api/v1/ai/status": () => ({ available: true, features: on ? { [RECONCILE]: info } : {} }),
    "GET /api/v1/models/account.journal": () => ({ items: [{ id: 1, display_name: "Bank (BNK1)" }], total: 1 }),
    "POST /api/v1/models/account.bank_statement_line/call/get_reconcile_lines": () => ({ result: [line] }),
    "POST /api/v1/models/account.bank_statement_line/7/call/get_reconcile_info": () => ({
      result: { ...line, narration: null, counterparts: [] },
    }),
    "POST /api/v1/models/account.bank_statement_line/7/call/get_suggestions": () => ({ result: [] }),
    "POST /api/v1/models/account.bank_statement_line/7/call/search_candidates": () => ({ result: [] }),
    ...extra,
  });
}

describe("AI suggestions in bank reconciliation", () => {
  it("ranks candidates for the selected transaction and accepts the match proposal", async () => {
    const { calls } = routes(true, {
      "POST /api/v1/account_bank/ai/lines/7/suggest": () => ({
        run_id: 8,
        used_model: false,
        ai_generated: true,
        suggestions: [
          {
            index: 0,
            kind: "items",
            line_ids: [55],
            items: [
              {
                id: 55,
                move_name: "INV/2026/00012",
                partner: "Lyon Robotics SA",
                amount_residual: 1200,
                currency: "EUR",
              },
            ],
            amount: 1200,
            score: 0.93,
            reasons: ["amount_exact", "reference_similar", "partner_named"],
            rule: null,
          },
          {
            index: 1,
            kind: "items",
            line_ids: [56],
            items: [
              {
                id: 56,
                move_name: "INV/2026/00013",
                partner: "Lyon Robotics SA",
                amount_residual: 1200,
                currency: "EUR",
              },
            ],
            amount: 1200,
            score: 0.41,
            reasons: ["amount_exact"],
            rule: null,
          },
        ],
        proposal,
      }),
      "POST /api/v1/ai/proposals/61/accept": () => ({ ...proposal, state: "accepted", ai_generated: false }),
    });
    wrap(clientAction("account_bank.reconcile")!());
    await userEvent.click(await screen.findByRole("button", { name: /VIR LYON ROBOTICS/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Suggest a match" }));
    expect(await screen.findByText("INV/2026/00012")).toBeInTheDocument();
    expect(screen.getByText(/confidence 93 %/)).toHaveTextContent("partner named in the label");
    expect(screen.getByText("✦ AI suggestion")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/ai/proposals/61/accept")).toBe(true));
    expect(await screen.findByText("Match proposal decided")).toBeInTheDocument();
  });

  it("proposes matches for the open transactions of the journal", async () => {
    const { calls } = routes(true, {
      "POST /api/v1/account_bank/ai/suggest": () => ({ proposed: 3, rows: [], ai_generated: true }),
    });
    wrap(clientAction("account_bank.reconcile")!());
    await screen.findByRole("option", { name: "Bank (BNK1)" });
    await userEvent.selectOptions(screen.getByLabelText("Journal"), "1");
    await userEvent.click(await screen.findByRole("button", { name: "AI: propose matches" }));
    expect(await screen.findByText("3 match proposals to review")).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/v1/account_bank/ai/suggest")?.body).toEqual({ journal_id: 1 });
  });

  it("hides the AI buttons when the feature is off and reports errors", async () => {
    routes(false);
    const off = wrap(clientAction("account_bank.reconcile")!());
    await userEvent.click(await screen.findByRole("button", { name: /VIR LYON ROBOTICS/ }));
    await screen.findByText("No suggestion.");
    expect(screen.queryByRole("button", { name: "Suggest a match" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "AI: propose matches" })).not.toBeInTheDocument();
    off.unmount();
    routes(true, {
      "POST /api/v1/account_bank/ai/lines/7/suggest": () =>
        new Response(JSON.stringify({ code: "feature_disabled", message: "off" }), { status: 404 }),
    });
    wrap(clientAction("account_bank.reconcile")!());
    await userEvent.click(await screen.findByRole("button", { name: /VIR LYON ROBOTICS/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Suggest a match" }));
    expect(await screen.findByText("This AI feature is turned off.")).toBeInTheDocument();
  });
});
