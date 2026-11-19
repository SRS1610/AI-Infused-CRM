import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { mockFetch } from "../../test/mockApi";
import type { Proposal } from "../../kernel/ai/api";
import { formPanelsFor } from "../../kernel/extensions";
import { DiscountSuggestions, LinesFromText, PricingFlags, UpsellSuggestions } from "./SaleAi";

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );

const status =
  (features: string[], available = true) =>
  () => ({ available, features: Object.fromEntries(features.map((f) => [f, { title: f }])) });

const proposal = (over: Partial<Proposal> = {}): Proposal => ({
  id: 70,
  kind: "write",
  title: "Add Vacuum gripper kit to QT/1",
  res_model: "sale.quotation",
  res_id: 5,
  payload: { line_ids: [["create", { product_id: 9, quantity: 1 }]] },
  original: {},
  state: "pending",
  reason: null,
  ai_generated: true,
  run_id: 31,
  feature: "sale.upsell",
  ...over,
});

describe("sales AI client", () => {
  it("adds four panels to the quotation form", () => {
    expect(formPanelsFor("sale.quotation").map((p) => p.title)).toEqual([
      "Pricing check",
      "Suggested products",
      "Discount suggestions",
      "Lines from a request",
    ]);
  });

  it("hides the panels when the features are switched off", async () => {
    const { calls } = mockFetch({ "GET /api/v1/ai/status": status([]) });
    const { container } = wrap(
      <>
        <UpsellSuggestions resId={5} reload={() => {}} />
        <DiscountSuggestions resId={5} reload={() => {}} />
        <LinesFromText resId={5} reload={() => {}} />
        <PricingFlags resId={5} />
      </>,
    );
    await screen.findAllByText((_, el) => el?.classList.contains("ai-panel-hidden") ?? false);
    expect(container.querySelectorAll(".ai-panel-hidden")).toHaveLength(4);
    expect(calls.filter((c) => c.path.startsWith("/api/v1/sale/"))).toHaveLength(0);
  });

  it("lists suggestions with their order counts and proposes a line that waits for acceptance", async () => {
    const reload = vi.fn();
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(["sale.upsell"]),
      "GET /api/v1/sale/ai/quotations/5/upsell": () => ({
        run_id: 31,
        orders_learnt: 12,
        suggestions: [
          {
            product_id: 9,
            display_name: "Vacuum gripper kit",
            score: 0.6,
            price_unit: 1450,
            currency: "USD",
            source: "history",
            reasons: ["Bought with Collaborative robot arm in 3 of 5 orders"],
          },
        ],
      }),
      "POST /api/v1/sale/ai/quotations/5/upsell/propose": () => proposal(),
      "POST /api/v1/ai/proposals/70/accept": () => ({ ...proposal(), state: "accepted", ai_generated: false }),
    });
    wrap(<UpsellSuggestions resId={5} reload={reload} />);
    const list = await screen.findByRole("list", { name: "Suggested products" });
    expect(within(list).getByText("Bought with Collaborative robot arm in 3 of 5 orders")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add Vacuum gripper kit" }));
    expect(await screen.findByText("Add Vacuum gripper kit to QT/1")).toBeInTheDocument();
    expect(calls.find((c) => c.path.endsWith("/upsell/propose"))?.body).toEqual({
      product_id: 9,
      quantity: 1,
      run_id: 31,
    });
    expect(calls.some((c) => c.path.includes("/accept"))).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(reload).toHaveBeenCalled();
  });

  it("shows discounts inside their bands, one proposal per line", async () => {
    mockFetch({
      "GET /api/v1/ai/status": status(["sale.discount_suggest"]),
      "POST /api/v1/sale/ai/quotations/5/discounts": () => ({
        run_id: 40,
        lines: [
          {
            line_id: 1,
            product: "Collaborative robot arm",
            quantity: 6,
            current_discount: 0,
            status: "suggested",
            suggested_discount: 7.5,
            band: { id: 2, name: "Hardware", min: 3, max: 15 },
            reasons: ["Band Hardware: 3 to 15 %"],
            proposal: proposal({
              id: 71,
              title: "Discount 7.5 % on Collaborative robot arm (band 3–15 %)",
              res_model: "sale.quotation_line",
              res_id: 1,
              payload: { discount: 7.5 },
              original: { discount: 0 },
              feature: "sale.discount_suggest",
            }),
          },
          { line_id: 2, product: "Coffee", quantity: 1, current_discount: 0, status: "no_band", reasons: [] },
        ],
      }),
    });
    wrap(<DiscountSuggestions resId={5} reload={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Suggest discounts" }));
    const list = await screen.findByRole("list", { name: "Discount suggestions" });
    expect(within(list).getByText("7.5 % (band 3–15 %)")).toBeInTheDocument();
    expect(within(list).getByText("No approved band")).toBeInTheDocument();
    expect(screen.getByText("Discount 7.5 % on Collaborative robot arm (band 3–15 %)")).toBeInTheDocument();
  });

  it("maps a pasted request to lines and lists what was not matched", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(["sale.quote_from_text"]),
      "POST /api/v1/ai/features/sale.quote_from_text/run": () => ({
        run_id: 50,
        feature: "sale.quote_from_text",
        text: "",
        ai_generated: true,
        citations: [],
        flags: [],
        status: "ok",
        data: {
          lines: [
            {
              index: 0,
              description: "robot arms",
              product_id: 3,
              display_name: "Collaborative robot arm",
              quantity: 2,
              method: "name",
              notes: [],
            },
          ],
          flagged: [
            {
              index: 1,
              description: "monitors",
              reason: "ambiguous",
              candidates: [
                { product_id: 7, display_name: "Monitor 24 inch", score: 0.5 },
                { product_id: 8, display_name: "Monitor 27 inch", score: 0.5 },
              ],
            },
            { index: 2, description: "teleporter", reason: "no_match", candidates: [] },
          ],
        },
        proposals: [
          proposal({ id: 72, title: "Add 1 lines from the request to QT/1", feature: "sale.quote_from_text" }),
        ],
      }),
    });
    wrap(<LinesFromText resId={5} reload={() => {}} />);
    await userEvent.type(await screen.findByLabelText("Customer request (e-mail, RFP, list)"), "2 robot arms");
    await userEvent.click(screen.getByRole("button", { name: "Find the products" }));
    const matched = await screen.findByRole("list", { name: "Matched lines" });
    expect(within(matched).getByText("Collaborative robot arm")).toBeInTheDocument();
    const flagged = screen.getByRole("list", { name: "Not matched" });
    expect(within(flagged).getByText("Several products fit")).toBeInTheDocument();
    expect(within(flagged).getByText("Not in the catalogue")).toBeInTheDocument();
    expect(within(flagged).getByText("Monitor 24 inch · Monitor 27 inch")).toBeInTheDocument();
    expect(screen.getByText("Add 1 lines from the request to QT/1")).toBeInTheDocument();
    expect(calls.find((c) => c.path.endsWith("/run"))?.body).toEqual({
      res_model: "sale.quotation",
      res_id: 5,
      inputs: { text: "2 robot arms" },
    });
  });

  it("shows pricing findings and approves a recorded flag", async () => {
    let state = "open";
    const { calls } = mockFetch({
      "GET /api/v1/ai/status": status(["sale.pricing_breach"]),
      "GET /api/v1/sale/ai/quotations/5/pricing": () => ({
        findings: [
          {
            line_id: 1,
            product: "Robot arm",
            code: "discount_above_band",
            severity: "breach",
            message: "Discount 25 % above the Hardware band (at most 15 %)",
          },
        ],
        flags: [
          {
            id: 3,
            product: "Robot arm",
            code: "discount_above_band",
            severity: "breach",
            message: "Discount 25 % above the Hardware band (at most 15 %)",
            state,
            approved_by: state === "approved" ? "Admin" : null,
          },
        ],
      }),
      "POST /api/v1/sale/ai/pricing-flags/3/approve": () => {
        state = "approved";
        return {};
      },
    });
    wrap(<PricingFlags resId={5} />);
    const findings = await screen.findByRole("list", { name: "Pricing findings" });
    expect(within(findings).getByText("Breach")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("Approved by Admin")).toBeInTheDocument();
    expect(calls.some((c) => c.path === "/api/v1/sale/ai/pricing-flags/3/approve")).toBe(true);
  });

  it("explains an error such as a switched-off feature", async () => {
    mockFetch({
      "GET /api/v1/ai/status": status(["sale.pricing_breach"]),
      "GET /api/v1/sale/ai/quotations/5/pricing": () =>
        new Response(JSON.stringify({ code: "feature_disabled", message: "off" }), { status: 404 }),
    });
    wrap(<PricingFlags resId={5} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("This AI feature is turned off.");
  });
});
