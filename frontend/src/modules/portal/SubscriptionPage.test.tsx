import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { SessionProvider } from "../../kernel/session";
import { PortalApp } from "./PortalApp";
import { SubscriptionPage, subscriptionStateLabel, type PortalSubscription } from "./SubscriptionPage";

const subscription: PortalSubscription = {
  id: 7,
  name: "QT/2026/00007",
  state: "progress",
  state_label: "In progress",
  plan: "Monthly",
  billing_period: { value: 1, unit: "month", unit_label: "Months" },
  start_date: "2026-09-01",
  next_invoice_date: "2026-12-01",
  end_date: null,
  closing: false,
  currency: "USD",
  recurring_total: 50,
  recurring_total_taxed: 60,
  payment_method: "Test card •••• 4242",
  can_close: true,
  close_reasons: [
    { id: 1, name: "Too expensive" },
    { id: 4, name: "No longer needed" },
  ],
  lines: [{ id: 3, name: "Cloud seat", quantity: 2, price_unit: 25, discount: 0, price_subtotal: 50 }],
  invoices: [
    {
      id: 11,
      name: "INV/2026/00011",
      move_type: "out_invoice",
      invoice_date: "2026-11-01",
      period_start: "2026-11-01",
      period_end: "2026-11-30",
      amount_total: 60,
      payment_state: "paid",
      url: "/portal/invoices/11?token=abc",
    },
  ],
};

const anonymous = () => new Response(JSON.stringify({ error: "AccessDenied", message: "no" }), { status: 401 });

const wrap = (ui: React.ReactNode, path: string) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SessionProvider>
        <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
      </SessionProvider>
    </QueryClientProvider>,
  );

describe("portal subscriptions", () => {
  it("shows the status and next invoice, and closes at the period end", async () => {
    const bodies: any[] = [];
    mockFetch({
      "GET /api/v1/auth/session": anonymous,
      "GET /api/v1/sale/subscriptions/7": (url) =>
        url.searchParams.get("token") === "tok" ? subscription : new Response("{}", { status: 401 }),
      "POST /api/v1/sale/subscriptions/7/close": (_u, init) => {
        bodies.push(JSON.parse(String(init.body)));
        return { ...subscription, closing: true, can_close: false, end_date: "2026-11-30" };
      },
    });
    wrap(
      <Routes>
        <Route path="/portal/subscriptions/:id" element={<SubscriptionPage />} />
      </Routes>,
      "/portal/subscriptions/7?token=tok",
    );
    expect(await screen.findByRole("heading", { name: "Subscription QT/2026/00007" })).toBeInTheDocument();
    expect(screen.getByText("2026-12-01")).toBeInTheDocument();
    expect(screen.getAllByText("$60.00").length).toBeGreaterThan(0);
    expect(screen.getByText("Billed monthly", { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "INV/2026/00011" })).toHaveAttribute(
      "href",
      "/portal/invoices/11?token=abc",
    );
    await userEvent.click(screen.getByRole("button", { name: "Close my subscription" }));
    await userEvent.selectOptions(screen.getByLabelText("Why are you leaving?"), "4");
    await userEvent.type(screen.getByLabelText("Comment (optional)"), "Moving on");
    await userEvent.click(screen.getByRole("button", { name: "Confirm closing" }));
    expect(await screen.findByText("Your subscription ends on 2026-11-30.")).toBeInTheDocument();
    expect(bodies[0]).toEqual({ reason_id: 4, note: "Moving on", token: "tok" });
    expect(screen.queryByRole("button", { name: "Close my subscription" })).not.toBeInTheDocument();
  });

  it("hides closing when the plan does not allow it and shows errors", async () => {
    mockFetch({
      "GET /api/v1/auth/session": anonymous,
      "GET /api/v1/sale/subscriptions/7": () => ({
        ...subscription,
        can_close: false,
        close_reasons: [],
        billing_period: { value: 3, unit: "month", unit_label: "Months" },
        invoices: [],
      }),
      "GET /api/v1/sale/subscriptions/8": () =>
        new Response(JSON.stringify({ error: "MissingError", message: "Subscription not found" }), { status: 404 }),
    });
    wrap(
      <Routes>
        <Route path="/portal/subscriptions/:id" element={<SubscriptionPage />} />
      </Routes>,
      "/portal/subscriptions/7",
    );
    expect(await screen.findByText("Billed every 3 Months", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("No invoices yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close my subscription" })).not.toBeInTheDocument();
    expect(subscriptionStateLabel("churned")).toBe("Ended");
    expect(subscriptionStateLabel("other")).toBe("other");
  });

  it("reports a missing subscription", async () => {
    mockFetch({
      "GET /api/v1/auth/session": anonymous,
      "GET /api/v1/sale/subscriptions/8": () =>
        new Response(JSON.stringify({ error: "MissingError", message: "Subscription not found" }), { status: 404 }),
    });
    wrap(
      <Routes>
        <Route path="/portal/subscriptions/:id" element={<SubscriptionPage />} />
      </Routes>,
      "/portal/subscriptions/8",
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("Subscription not found");
  });

  it("lists subscriptions on the documents page when the module is installed", async () => {
    mockFetch({
      "GET /api/v1/auth/session": () => ({
        user: { id: 9, login: "c@x", name: "Cust", lang: "en_US", tz: "UTC", share: true },
        company: { id: 1, name: "My Company" },
        allowed_companies: [],
        selected_companies: [],
        groups: [],
        csrf_token: "c",
        lang: "en_US",
        modules: ["portal", "sale_subscription"],
      }),
      "GET /api/v1/portal/documents": () => ({ quotations: [] }),
      "GET /api/v1/sale/subscriptions": () => ({
        subscriptions: [
          {
            id: 7,
            name: "QT/2026/00007",
            plan: "Monthly",
            state: "paused",
            next_invoice_date: null,
            recurring_total: 50,
            recurring_total_taxed: 60,
            currency: "USD",
          },
        ],
      }),
      "GET /api/v1/sale/subscriptions/7": () => subscription,
    });
    wrap(<PortalApp />, "/portal");
    const link = await screen.findByRole("link", { name: "QT/2026/00007" });
    expect(screen.getByText("Paused")).toBeInTheDocument();
    await userEvent.click(link);
    expect(await screen.findByRole("heading", { name: "Subscription QT/2026/00007" })).toBeInTheDocument();
  });
});
