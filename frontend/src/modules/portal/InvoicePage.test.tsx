import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import { mockFetch } from "../../test/mockApi";
import { InvoicePage, paymentStateLabel, type PaymentOptions, type PortalInvoice } from "./InvoicePage";

const invoice: PortalInvoice = {
  id: 12,
  name: "INV/2026/00012",
  move_type: "out_invoice",
  state: "posted",
  payment_state: "not_paid",
  invoice_date: "2026-11-01",
  invoice_date_due: "2026-12-01",
  customer: "Acme Robotics",
  company: { name: "My Company", email: null, phone: null },
  currency: "USD",
  amount_untaxed: 200,
  amount_tax: 17,
  amount_total: 217,
  amount_residual: 217,
  payment_reference: "INV/2026/00012",
  lines: [
    {
      id: 1,
      display_type: "line_section",
      name: "Hardware",
      quantity: 0,
      uom: null,
      price_unit: 0,
      discount: 0,
      taxes: [],
      price_subtotal: 0,
    },
    {
      id: 2,
      display_type: "product",
      name: "Gripper",
      quantity: 2,
      uom: "Units",
      price_unit: 100,
      discount: 0,
      taxes: ["8.5%"],
      price_subtotal: 200,
    },
  ],
};

const options: PaymentOptions = {
  can_pay: true,
  providers: [
    { id: 4, name: "Card", code: "stripe", test_mode: false, can_save: true },
    { id: 5, name: "Test card (no real payment)", code: "test", test_mode: true, can_save: false },
  ],
  transactions: [],
};

const wrap = (path: string) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/portal/invoices/:id" element={<InvoicePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );

describe("portal invoice page", () => {
  it("shows the invoice and pays it with the chosen provider", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    const { calls } = mockFetch({
      "GET /api/v1/account/invoices/12": () => invoice,
      "GET /api/v1/payment/invoices/12": () => options,
      "POST /api/v1/payment/invoices/12/pay": () => ({
        reference: "INV-1",
        redirect_url: "https://checkout.test/s",
        state: "pending",
      }),
    });
    wrap("/portal/invoices/12?token=tok");
    expect(await screen.findByRole("heading", { name: "Invoice INV/2026/00012" })).toBeInTheDocument();
    expect(screen.getByText("To pay")).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "Gripper" })).toBeInTheDocument();
    await userEvent.selectOptions(await screen.findByLabelText("Pay with"), "4");
    await userEvent.click(screen.getByLabelText("Save my payment method"));
    await userEvent.click(screen.getByRole("button", { name: /Pay now/ }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://checkout.test/s"));
    const pay = calls.find((c) => c.path.endsWith("/pay"))!;
    expect(pay.body).toEqual({ token: "tok", provider_id: 4, save_method: true });
    vi.unstubAllGlobals();
  });

  it("shows the result of a payment and failures", async () => {
    mockFetch({
      "GET /api/v1/account/invoices/12": () => ({ ...invoice, payment_state: "paid", amount_residual: 0 }),
      "GET /api/v1/payment/invoices/12": () => ({ ...options, can_pay: false, providers: [] }),
      "GET /api/v1/payment/invoices/12/status": () => ({
        reference: "R",
        state: "done",
        message: null,
        payment_state: "paid",
      }),
    });
    wrap("/portal/invoices/12?token=tok&payment=R");
    expect(await screen.findByText("Thank you, your payment has been received.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Pay now/ })).not.toBeInTheDocument();
    expect(paymentStateLabel("in_payment")).toBe("Payment received, being processed");
    expect(paymentStateLabel("other")).toBe("other");
  });

  it("reports a declined payment and an unknown invoice", async () => {
    mockFetch({
      "GET /api/v1/account/invoices/12": () => invoice,
      "GET /api/v1/payment/invoices/12": () => options,
      "GET /api/v1/payment/invoices/12/status": () => ({
        reference: "R",
        state: "error",
        message: "Card declined",
        payment_state: "not_paid",
      }),
      "POST /api/v1/payment/invoices/12/pay": () =>
        new Response(JSON.stringify({ error: "UserError", message: "Online payment is not available" }), {
          status: 400,
        }),
    });
    wrap("/portal/invoices/12?token=tok&payment=R");
    expect(await screen.findByText("Card declined")).toBeInTheDocument();
    await userEvent.selectOptions(await screen.findByLabelText("Pay with"), "5");
    expect(screen.getByText("Test mode: no real payment is taken.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Pay now/ }));
    expect(await screen.findByText("Online payment is not available")).toBeInTheDocument();
  });

  it("shows an error for a bad link", async () => {
    mockFetch({
      "GET /api/v1/account/invoices/99": () =>
        new Response(JSON.stringify({ error: "AccessDenied", message: "Invalid or expired link" }), { status: 401 }),
    });
    wrap("/portal/invoices/99?token=bad");
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid or expired link");
  });
});
