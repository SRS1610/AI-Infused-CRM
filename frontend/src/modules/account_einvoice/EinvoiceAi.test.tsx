import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { formPanelsFor } from "../../kernel/extensions";
import { ERROR_EXPLAIN } from "./EinvoiceAi";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const info = {
  title: "E-invoice error explanations",
  task_class: "summarize",
  interactive: true,
  writes: false,
  outbound: false,
  disclosure: "ai_generated",
  risk_class: "minimal",
};
const status = (on = true, available = true) => ({ available, features: on ? { [ERROR_EXPLAIN]: info } : {} });

function panel(record: Record<string, unknown> = { state: "error" }) {
  const p = formPanelsFor("account.edi_document").find((x) => x.title === "Explain e-invoice errors (AI)");
  expect(p).toBeDefined();
  return wrap(p!.render({ model: "account.edi_document", resId: 5, record, reload: () => undefined }));
}

describe("E-invoice error explanations", () => {
  it("explains each rule with the field to fix and a link to the record", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "POST /api/v1/account_einvoice/ai/documents/5/explain": () => ({
        run_id: 3,
        removed_numbers: [],
        items: [
          {
            rule_id: "PEPPOL-EN16931-R010",
            flag: "fatal",
            count: 1,
            label: "Customer: Peppol address",
            message: "Buyer electronic address MUST be provided",
            explanation: "The customer has no Peppol address.",
            fix: "Fill in the customer's Peppol address.",
            explained_by: "model",
            link: { model: "contacts.partner", res_id: 44, field: "peppol_endpoint" },
          },
          {
            rule_id: "BR-25",
            flag: "fatal",
            count: 2,
            label: "Invoice line: description",
            message: "Each Invoice line shall contain the Item name",
            explanation: "Each Invoice line shall contain the Item name",
            fix: "Correct the invoice line, then generate the e-invoice again.",
            explained_by: "catalogue",
            link: { model: "account.move_line", res_id: 90, field: "name", move_id: 12 },
          },
        ],
      }),
      "POST /api/v1/ai/runs/3/feedback": () => ({ id: 1 }),
    });
    panel();
    await userEvent.click(await screen.findByRole("button", { name: "Explain the errors" }));
    expect(await screen.findByText("The customer has no Peppol address.")).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: "Open the record to fix" });
    expect(links[0]).toHaveAttribute("href", "/m/contacts.partner/44");
    expect(links[1]).toHaveAttribute("href", "/m/account.move/12");
    expect(screen.getAllByText("✦ AI explanation")).toHaveLength(1);
    expect(screen.getByText("×2")).toBeInTheDocument();
    await userEvent.click(screen.getByTitle("Helpful"));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/v1/ai/runs/3/feedback")).toBe(true));
  });

  it("is hidden for documents without errors and when off; shows server errors", async () => {
    mockFetch({ "GET /api/v1/ai/status": () => status() });
    const sent = panel({ state: "sent" });
    expect(sent.container.querySelector(".ai-panel-hidden")).not.toBeNull();
    sent.unmount();
    mockFetch({ "GET /api/v1/ai/status": () => status(false) });
    const off = panel();
    await waitFor(() => expect(off.container.querySelector(".ai-panel-hidden")).not.toBeNull());
    off.unmount();
    mockFetch({
      "GET /api/v1/ai/status": () => status(true, false),
      "POST /api/v1/account_einvoice/ai/documents/5/explain": () =>
        new Response(JSON.stringify({ code: "feature_disabled", message: "off" }), { status: 404 }),
    });
    panel({ state: "rejected" });
    expect(await screen.findByText("AI unavailable")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Explain the errors" }));
    expect(await screen.findByText("This AI feature is turned off.")).toBeInTheDocument();
  });

  it("says when there is nothing to explain", async () => {
    mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "POST /api/v1/account_einvoice/ai/documents/5/explain": () => ({ run_id: 1, removed_numbers: [], items: [] }),
    });
    panel();
    await userEvent.click(await screen.findByRole("button", { name: "Explain the errors" }));
    expect(await screen.findByText("This e-invoice has no error.")).toBeInTheDocument();
  });
});
