import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import { formPanelsFor } from "../../kernel/extensions";
import { ANOMALY, FORECAST } from "./RentalAi";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const status = (names = [FORECAST, ANOMALY]) => ({
  available: true,
  features: Object.fromEntries(
    names.map((n) => [
      n,
      {
        title: n,
        task_class: "summarize",
        interactive: true,
        writes: false,
        outbound: false,
        disclosure: "ai_generated",
        risk_class: "minimal",
      },
    ]),
  ),
});

const forecast = (weeks = 2) => ({
  product_id: 3,
  product: "Projector",
  units: 10,
  method: "weekly profile x damped robust trend",
  history_days: 182,
  trend_per_week: 0.1,
  recent_weeks: [40, 42, 38, 45],
  weeks: Array.from({ length: weeks }, (_, i) => ({
    week: i + 1,
    week_start: `2026-11-${String(2 + 7 * i).padStart(2, "0")}`,
    week_end: `2026-11-${String(8 + 7 * i).padStart(2, "0")}`,
    unit_days: 44 + i,
    average: 6.3,
    low: 38,
    high: 50,
    utilisation: 0.63,
    booked_unit_days: 20 - i,
  })),
  run_id: 70,
});

const alert = {
  id: 5,
  kind: "late_returns",
  kind_label: "Repeated late returns",
  summary: "Acme returned 3 of their last 4 rentals late (up to 30 h)",
  detected_on: "2026-11-02",
  state: "open",
};

const panelFor = (model: string, title: string, record: Record<string, unknown>, resId = 3) => {
  const p = formPanelsFor(model).find((x) => x.title === title);
  expect(p).toBeDefined();
  return wrap(p!.render({ model, resId, record, reload: () => undefined }));
};

describe("Rental AI panels", () => {
  it("shows the weekly forecast beside what is booked, and the product's alerts", async () => {
    let dismissed = 0;
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": () => status(),
      "GET /api/v1/sale_rental/ai/products/3/forecast": (url) => forecast(Number(url.searchParams.get("weeks"))),
      "GET /api/v1/sale_rental/ai/alerts": () => ({ alerts: dismissed ? [] : [alert] }),
      "POST /api/v1/sale_rental/ai/alerts/5/dismiss": () => {
        dismissed += 1;
        return { ...alert, state: "dismissed" };
      },
    });
    panelFor("product.product", "Rental demand (AI)", { rent_ok: true });
    const table = await screen.findByRole("table");
    expect(within(table).getByText("2026-11-02")).toBeInTheDocument();
    expect(within(table).getAllByText("63 %")).toHaveLength(4);
    expect(screen.getByText("✦ AI forecast")).toBeInTheDocument();
    expect(screen.getByText(/182 days of history/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Weeks ahead"), "8");
    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(9));
    expect(calls.some((c) => c.url.searchParams.get("weeks") === "8")).toBe(true);
    expect(await screen.findByText(alert.summary)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: `Dismiss: ${alert.summary}` }));
    expect(await screen.findByText("No open usage alerts.")).toBeInTheDocument();
    expect(dismissed).toBe(1);
  });

  it("shows the alerts of a rental order and hides itself elsewhere", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": () => status([ANOMALY]),
      "GET /api/v1/sale_rental/ai/alerts": () => ({ alerts: [alert] }),
    });
    panelFor("sale.quotation", "Rental alerts (AI)", { is_rental_order: true }, 8);
    expect(await screen.findByText(alert.summary)).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/v1/sale_rental/ai/alerts")?.url.searchParams.get("order_id")).toBe("8");
    const other = panelFor("sale.quotation", "Rental alerts (AI)", { is_rental_order: false }, 9);
    expect(other.container.querySelector(".ai-panel-hidden")).not.toBeNull();
  });

  it("is hidden when both features are off or the product is not rented", async () => {
    const { calls } = mockFetch({ "GET /api/v1/ai/status": () => status([]) });
    const { container } = panelFor("product.product", "Rental demand (AI)", { rent_ok: true });
    await waitFor(() => expect(container.querySelector(".ai-panel-hidden")).not.toBeNull());
    expect(calls.some((c) => c.path.includes("/sale_rental/ai/"))).toBe(false);
    const sold = panelFor("product.product", "Rental demand (AI)", { rent_ok: false });
    expect(sold.container.querySelector(".ai-panel-hidden")).not.toBeNull();
  });

  it("shows the reason when the forecast is refused", async () => {
    mockFetch({
      "GET /api/v1/ai/status": () => status([FORECAST]),
      "GET /api/v1/sale_rental/ai/products/3/forecast": () =>
        new Response(JSON.stringify({ code: "feature_disabled", message: "off" }), { status: 404 }),
    });
    panelFor("product.product", "Rental demand (AI)", { rent_ok: true });
    expect(await screen.findByText("This AI feature is turned off.")).toBeInTheDocument();
  });
});
