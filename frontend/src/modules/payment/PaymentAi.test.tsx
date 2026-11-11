import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { formPanelsFor } from "../../kernel/extensions";
import { FAILURES } from "./PaymentAi";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const info = {
  title: "Payment failure summary",
  task_class: "summarize",
  interactive: true,
  writes: false,
  outbound: false,
  disclosure: "ai_generated",
  risk_class: "minimal",
};
const status = (on = true, available = true) => ({ available, features: on ? { [FAILURES]: info } : {} });

function panel(model: string, title: string, record: Record<string, unknown>) {
  const p = formPanelsFor(model).find((x) => x.title === title);
  expect(p).toBeDefined();
  return wrap(p!.render({ model, resId: 9, record, reload: () => undefined }));
}

describe("Payment failure on a transaction", () => {
  it("shows the reason and the next step", async () => {
    mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "GET /api/v1/payment/ai/transactions/9/failure": () => ({
        run_id: 1,
        category: "card_expired",
        label: "Card expired",
        advice: "Ask the customer to update the saved card or pay with a new one.",
        code: "expired_card",
        message: "decline_code: expired_card",
      }),
    });
    panel("payment.transaction", "Why did this payment fail? (AI)", { state: "error" });
    expect(await screen.findByText("Card expired")).toBeInTheDocument();
    expect(screen.getByText(/update the saved card/)).toBeInTheDocument();
    expect(screen.getByText("Provider code: expired_card")).toBeInTheDocument();
    expect(screen.getByText("✦ AI classification")).toBeInTheDocument();
  });

  it("is hidden for successful payments and when the feature is off", async () => {
    mockFetch({ "GET /api/v1/ai/status": () => status() });
    const done = panel("payment.transaction", "Why did this payment fail? (AI)", { state: "done" });
    expect(done.container.querySelector(".ai-panel-hidden")).not.toBeNull();
    done.unmount();
    mockFetch({ "GET /api/v1/ai/status": () => status(false) });
    const off = panel("payment.transaction", "Why did this payment fail? (AI)", { state: "error" });
    await waitFor(() => expect(off.container.querySelector(".ai-panel-hidden")).not.toBeNull());
  });

  it("explains an error from the server", async () => {
    mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "GET /api/v1/payment/ai/transactions/9/failure": () =>
        new Response(JSON.stringify({ code: "feature_disabled", message: "off" }), { status: 404 }),
    });
    panel("payment.transaction", "Why did this payment fail? (AI)", { state: "cancel" });
    expect(await screen.findByText("This AI feature is turned off.")).toBeInTheDocument();
  });
});

describe("Payment failures of a provider", () => {
  it("lists customers with the main reason and summarises with the AI label", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "GET /api/v1/payment/ai/failures": () => ({
        run_id: 2,
        failures: 3,
        since: "2026-08-13",
        by_category: { insufficient_funds: 2, card_expired: 1 },
        labels: { insufficient_funds: "Insufficient funds", card_expired: "Card expired" },
        customers: [
          {
            partner_id: 5,
            customer: "Acme",
            failures: 3,
            amounts: { USD: 250 },
            main_label: "Insufficient funds",
            advice: "Ask the customer to pay again in a few days.",
            last_failure: "2026-11-01",
          },
        ],
      }),
      "POST /api/v1/payment/ai/failures/narrative": () => ({
        run_id: 3,
        narrative: "Acme had 3 failed payments, mostly insufficient funds.",
        removed_numbers: [],
      }),
    });
    panel("payment.provider", "Payment failures (AI)", {});
    expect(await screen.findByText("Acme")).toBeInTheDocument();
    expect(screen.getByText(/3 failed payments since 2026-08-13/)).toBeInTheDocument();
    const summary = calls.find((c) => c.path === "/api/v1/payment/ai/failures");
    expect(summary?.url.searchParams.get("provider_id")).toBe("9");
    await userEvent.click(screen.getByRole("button", { name: "Summarise all failures" }));
    expect(await screen.findByText(/mostly insufficient funds\./)).toBeInTheDocument();
  });

  it("says when there is nothing to report and when AI is unavailable", async () => {
    mockFetch({
      "GET /api/v1/ai/status": () => status(true, false),
      "GET /api/v1/payment/ai/failures": () => ({
        run_id: 2,
        failures: 0,
        since: "2026-08-13",
        by_category: {},
        labels: {},
        customers: [],
      }),
    });
    panel("payment.provider", "Payment failures (AI)", {});
    expect(await screen.findByText("No failed payment in the last 90 days.")).toBeInTheDocument();
  });
});
