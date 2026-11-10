import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { SessionProvider } from "../../kernel/session";
import { InvitePage, PortalApp } from "./PortalApp";
import { QuotePage, type Quote } from "./QuotePage";

const quote: Quote = {
  id: 5,
  name: "QT/2026/00005",
  state: "sent",
  date_order: "2026-11-01T10:00:00",
  validity_date: "2026-12-01",
  customer: "Acme Robotics, Jordan Lee",
  company: { name: "My Company", email: "sales@my.example", phone: null },
  salesperson: "Admin",
  currency: "USD",
  amount_untaxed: 200,
  amount_tax: 40,
  amount_total: 240,
  note: "<p>Net 30</p>",
  signed_by: null,
  signed_at: null,
  lines: [
    {
      id: 1,
      display_type: "section",
      name: "Hardware",
      quantity: 0,
      uom: null,
      price_unit: 0,
      discount: 0,
      taxes: [],
      price_subtotal: 0,
      is_optional: false,
    },
    {
      id: 2,
      display_type: null,
      name: "Gripper",
      quantity: 2,
      uom: "Unit(s)",
      price_unit: 100,
      discount: 0,
      taxes: ["VAT 20%"],
      price_subtotal: 200,
      is_optional: false,
    },
  ],
};

const wrap = (ui: React.ReactNode, path: string) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SessionProvider>
        <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
      </SessionProvider>
    </QueryClientProvider>,
  );

describe("customer portal", () => {
  it("shows a quotation by token and accepts it", async () => {
    const bodies: any[] = [];
    mockFetch({
      "GET /api/v1/auth/session": () =>
        new Response(JSON.stringify({ error: "AccessDenied", message: "no" }), { status: 401 }),
      "GET /api/v1/sale/quotes/5": (url) =>
        url.searchParams.get("token") === "tok" ? quote : new Response("{}", { status: 401 }),
      "POST /api/v1/sale/quotes/5/accept": (_u, init) => {
        bodies.push(JSON.parse(String(init.body)));
        return { ...quote, state: "accepted", signed_by: "Jordan Lee" };
      },
    });
    wrap(
      <Routes>
        <Route path="/portal/quotes/:id" element={<QuotePage />} />
      </Routes>,
      "/portal/quotes/5?token=tok",
    );
    expect(await screen.findByRole("heading", { name: "Quotation QT/2026/00005" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download PDF" })).toHaveAttribute(
      "href",
      "/api/v1/sale/quotes/5/pdf?token=tok",
    );
    expect(screen.getByText("$240.00")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    await userEvent.type(screen.getByLabelText(/Type your full name/), "Jordan Lee");
    await userEvent.click(screen.getByRole("button", { name: "Accept and sign" }));
    expect(await screen.findByText(/Accepted by Jordan Lee/)).toBeInTheDocument();
    expect(bodies[0]).toEqual({ name: "Jordan Lee", token: "tok" });
  });

  it("shows taxes and lets the customer add an optional product", async () => {
    const bodies: any[] = [];
    const withOption: Quote = {
      ...quote,
      can_add_options: true,
      payment_terms: "30 days",
      lines: [
        ...quote.lines,
        {
          id: 3,
          display_type: null,
          name: "Extended warranty",
          quantity: 1,
          uom: null,
          price_unit: 50,
          discount: 0,
          taxes: ["VAT 20%"],
          price_subtotal: 50,
          is_optional: true,
        },
      ],
    };
    mockFetch({
      "GET /api/v1/auth/session": () =>
        new Response(JSON.stringify({ error: "AccessDenied", message: "no" }), { status: 401 }),
      "GET /api/v1/sale/quotes/5": () => withOption,
      "POST /api/v1/sale/quotes/5/optional/3": (_u, init) => {
        bodies.push(JSON.parse(String(init.body)));
        return {
          ...withOption,
          amount_untaxed: 250,
          amount_total: 300,
          lines: withOption.lines.map((l) => (l.id === 3 ? { ...l, is_optional: false } : l)),
        };
      },
    });
    wrap(
      <Routes>
        <Route path="/portal/quotes/:id" element={<QuotePage />} />
      </Routes>,
      "/portal/quotes/5?token=tok",
    );
    expect(await screen.findByText("Payment terms: 30 days")).toBeInTheDocument();
    expect(screen.getAllByText("VAT 20%")).toHaveLength(2);
    await userEvent.click(screen.getByRole("button", { name: "Add Extended warranty to my order" }));
    expect(await screen.findByText("$300.00")).toBeInTheDocument();
    expect(bodies[0]).toEqual({ token: "tok" });
    expect(screen.queryByRole("button", { name: /to my order/ })).not.toBeInTheDocument();
  });

  it("lists documents and updates the profile for portal users", async () => {
    const patches: any[] = [];
    mockFetch({
      "GET /api/v1/auth/session": () => ({
        user: { id: 9, login: "c@x", name: "Cust", lang: "en_US", tz: "UTC", share: true },
        company: { id: 1, name: "My Company" },
        allowed_companies: [],
        selected_companies: [],
        groups: [],
        csrf_token: "c",
        lang: "en_US",
        modules: ["portal"],
      }),
      "GET /api/v1/portal/documents": () => ({
        quotations: [
          {
            id: 5,
            name: "QT/2026/00005",
            date_order: "2026-11-01T10:00:00",
            validity_date: "2026-12-01",
            amount_total: 240,
            currency: "USD",
            state: "sent",
          },
        ],
      }),
      "GET /api/v1/portal/me": () => ({ name: "Cust", email: "c@x", company: "Acme", phone: null, city: "Paris" }),
      "PATCH /api/v1/portal/me": (_u, init) => {
        patches.push(JSON.parse(String(init.body)));
        return { name: "Cust", email: "c@x", company: "Acme", phone: "+33", city: "Paris" };
      },
    });
    wrap(<PortalApp />, "/portal");
    expect(await screen.findByRole("link", { name: "QT/2026/00005" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "My details" }));
    await userEvent.type(await screen.findByLabelText("Phone"), "+33");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    expect(patches[0]).toEqual({ phone: "+33" });
  });

  it("activates an invitation", async () => {
    mockFetch({ "POST /api/v1/portal/invite/accept": () => ({ login: "new@customer.example" }) });
    wrap(
      <Routes>
        <Route path="/portal/invite" element={<InvitePage />} />
      </Routes>,
      "/portal/invite?token=abc",
    );
    await userEvent.type(screen.getByLabelText("Choose a password"), "long-password");
    await userEvent.type(screen.getByLabelText("Repeat it"), "long-passwordX");
    await userEvent.click(screen.getByRole("button", { name: "Activate my account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("do not match");
    await userEvent.type(screen.getByLabelText("Repeat it"), "{backspace}");
    await userEvent.click(screen.getByRole("button", { name: "Activate my account" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Sign in as new@customer.example"));
  });
});
